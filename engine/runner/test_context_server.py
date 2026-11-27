"""test_context_server.py 用官方 MCP 客户端核对只读冻结事实服务的协议与边界。"""

import contextlib
import functools
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from review_context import materialize_context


def frozen(text, oid="frozen-id", mode="100644"):
    """frozen() 为协议测试提供显式已冻结的正文，不读取宿主项目或实际 Git 索引。"""
    return {"text": text, "oid": oid, "mode": mode}


def with_session(test):
    """with_session() 将 SDK 连接的进入、测试与退出放在同一任务，保持 AnyIO 取消域配对。"""
    @functools.wraps(test)
    async def run(self):
        """run() 在当前测试任务内创建并关闭 stdio 子进程，不跨测试生命周期转移连接。"""
        async with self.connected_session():
            await test(self)
    return run


class ContextServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-context-server-")
        self.base = Path(self.temporary.name)
        self.view = self.base / "view"
        self.audit = self.base / "access.jsonl"
        self.long_text = "前" * 16_100 + "针\r\nsecond a.*b\r\nthird\n"
        self.context = {
            "current": {"src/app.py": frozen("value = 7\nneedle = 'a.*b'\n", "current-object", "100755"),
                        "src/long.txt": frozen(self.long_text),
                        ".clarity/engine/file.py": frozen("tracked sidecar\n")},
            "baseline": {"src/app.py": frozen("value = 5\n", "baseline-object"),
                         "removed.txt": frozen("old a.*b\n", "removed-object")},
        }
        materialize_context(self.context, self.view)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.restore_permissions)

    def restore_permissions(self):
        """restore_permissions() 只恢复测试创建的投影权限，便于清理只读临时目录。"""
        if not self.view.exists():
            return
        self.view.chmod(0o700)
        for folder, directories, files in os.walk(self.view):
            for name in directories:
                path = Path(folder) / name
                if not path.is_symlink():
                    path.chmod(0o700)
            for name in files:
                path = Path(folder) / name
                if not path.is_symlink():
                    path.chmod(0o600)

    @contextlib.asynccontextmanager
    async def connected_session(self):
        """connected_session() 使用官方客户端完成握手，结束时在原任务关闭服务进程与流。"""
        async with contextlib.AsyncExitStack() as stack:
            errors = stack.enter_context((self.base / "server-stderr.log").open("w"))
            server = StdioServerParameters(
                command=sys.executable,
                args=[str(Path(__file__).with_name("context_server.py")), "--root", str(self.view), "--log", str(self.audit)],
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            read, write = await stack.enter_async_context(stdio_client(server, errlog=errors))
            self.session = await stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
            yield

    async def call(self, name, **arguments):
        """call() 让测试在真实 SDK 编解码与 stdio 边界之后核对结构化工具结果。"""
        reply = await self.session.call_tool(name, arguments)
        self.assertFalse(reply.isError, reply.content)
        self.assertIsInstance(reply.structuredContent, dict)
        self.assertEqual(reply.structuredContent, json.loads(reply.content[0].text))
        return reply.structuredContent

    @with_session
    async def test_only_three_readonly_tools_are_discoverable(self):
        tools = (await self.session.list_tools()).tools
        self.assertEqual({tool.name for tool in tools}, {"list_files", "read_file", "search"})
        for tool in tools:
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)
            self.assertFalse(tool.annotations.openWorldHint)
        read = next(tool for tool in tools if tool.name == "read_file")
        self.assertEqual(read.inputSchema["properties"]["version"]["enum"], ["current", "baseline"])
        self.assertEqual(read.inputSchema["properties"]["char_limit"]["maximum"], 16_000)

    @with_session
    async def test_inventory_pages_retain_original_paths_and_versions_without_source(self):
        page = await self.call("list_files", limit=1)
        self.assertEqual(page["files"][0]["path"], ".clarity/engine/file.py")
        self.assertEqual(page["next_offset"], 1)
        self.assertEqual(page["total"], 3)
        self.assertNotIn("text", page["files"][0])
        rest = await self.call("list_files", offset=page["next_offset"], limit=2)
        self.assertEqual([item["path"] for item in rest["files"]], ["src/app.py", "src/long.txt"])
        self.assertIsNone(rest["next_offset"])
        baseline = await self.call("list_files", version="baseline", prefix="src/")
        self.assertEqual(baseline["files"][0]["oid"], "baseline-object")

    @with_session
    async def test_reads_keep_source_identity_and_exact_original_text(self):
        current = await self.call("read_file", path="src/app.py")
        baseline = await self.call("read_file", path="src/app.py", version="baseline")
        self.assertEqual(current["text"], self.context["current"]["src/app.py"]["text"])
        self.assertEqual(current["oid"], "current-object")
        self.assertEqual(current["mode"], "100755")
        self.assertEqual(current["start_line"], 1)
        self.assertEqual(current["end_line"], 2)
        self.assertIsNone(current["next"])
        self.assertEqual(baseline["text"], "value = 5\n")
        self.assertEqual(baseline["oid"], "baseline-object")

    @with_session
    async def test_long_unicode_lines_can_be_reconstructed_across_protocol_pages(self):
        cursor = {"start_line": 1, "start_column": 1}
        pieces = []
        while cursor is not None:
            page = await self.call("read_file", path="src/long.txt", **cursor)
            self.assertLessEqual(len(page["text"]), 16_000)
            pieces.append(page["text"])
            cursor = page["next"]
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), self.long_text)
        self.assertIn("\r\n", "".join(pieces))

    @with_session
    async def test_search_is_literal_and_returns_original_coordinates_with_pagination(self):
        first = await self.call("search", query="a.*b", prefix="src/", limit=1)
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["matches"][0]["path"], "src/app.py")
        self.assertEqual(first["matches"][0]["line"], 2)
        second = await self.call("search", query="a.*b", prefix="src/", limit=1, offset=first["next_offset"])
        self.assertEqual(second["matches"][0]["path"], "src/long.txt")
        self.assertIsNone(second["next_offset"])
        baseline = await self.call("search", query="a.*b", version="baseline")
        self.assertEqual(baseline["matches"][0]["path"], "removed.txt")

    @with_session
    async def test_escape_alias_unknown_write_and_execution_requests_are_rejected(self):
        outside = self.base / "outside.txt"
        outside.write_text("OUTSIDE_SYNTHETIC_MUST_NOT_LEAK")
        for path in ("../outside.txt", str(outside), "current/src/app.py", "context.json", "src/absent.py", "C:\\outside.txt"):
            with self.subTest(path=path):
                result = await self.session.call_tool("read_file", {"path": path})
                self.assertTrue(result.isError)
                self.assertNotIn("OUTSIDE_SYNTHETIC_MUST_NOT_LEAK", str(result))
        for name in ("write_file", "exec", "create_terminal"):
            with self.subTest(name=name):
                result = await self.session.call_tool(name, {"path": "NEW_FILE", "command": "python src/app.py"})
                self.assertTrue(result.isError)
        self.assertFalse((self.view / "NEW_FILE").exists())

    @with_session
    async def test_invalid_ranges_and_types_are_errors_not_silent_clamping(self):
        for name, args in [("read_file", {"path": "src/app.py", "char_limit": 16_001}),
                           ("read_file", {"path": "src/app.py", "start_line": 0}),
                           ("read_file", {"path": "src/app.py", "line_count": True}),
                           ("list_files", {"limit": 101}),
                           ("list_files", {"offset": -1}),
                           ("search", {"query": "value", "version": "unknown"}),
                           ("search", {"query": ""})]:
            with self.subTest(name=name, args=args):
                result = await self.session.call_tool(name, args)
                self.assertTrue(result.isError)

    @with_session
    async def test_changed_projection_is_rejected_and_audit_omits_source_payload(self):
        await self.call("read_file", path="src/app.py")
        await self.call("search", query="a.*b")
        events = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual([item["tool"] for item in events], ["read_file", "search"])
        self.assertNotIn("value = 7", self.audit.read_text())
        self.assertNotIn("a.*b", self.audit.read_text())
        path = self.view / "current/src/app.py"
        path.chmod(0o600)
        path.write_text("value = 8\nneedle = 'a.*b'\n")
        result = await self.session.call_tool("read_file", {"path": "src/app.py"})
        self.assertTrue(result.isError)
        self.assertNotIn("value = 8", str(result))


if __name__ == "__main__":
    unittest.main()
