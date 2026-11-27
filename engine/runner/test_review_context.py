"""test_review_context.py 核对冻结正文、只读路径边界和可完整续读的有界查询。"""

import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from model_common import ModelFailure
from review_context import ContextStore, EXCERPT_CHARS, assess_delivery, make_reply_feedback, materialize_context


REPLY_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"], "additionalProperties": False}


def frozen(text, oid="recorded-oid", mode="100644"):
    return {"text": text, "oid": oid, "mode": mode}


class ReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-context-test-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "view"
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.restore_permissions)

    def restore_permissions(self):
        if self.root.is_symlink() or not self.root.is_dir():
            return
        self.root.chmod(0o700)
        for folder, directories, files in os.walk(self.root):
            for name in directories:
                path = Path(folder) / name
                if not path.is_symlink():
                    path.chmod(0o700)
            for name in files:
                path = Path(folder) / name
                if not path.is_symlink() and stat.S_ISREG(path.lstat().st_mode) and path.stat().st_nlink == 1:
                    path.chmod(0o600)

    def store(self, current=None, baseline=None, log_path=None):
        materialize_context({"current": current or {}, "baseline": baseline or {}}, self.root)
        return ContextStore(self.root, log_path)

    def write_events(self, events, *, include_session=True, cwd=None):
        path = self.base / "events.jsonl"
        session = {"type": "session/new", "session": {"sessionId": "test-session"},
                   "cwd": str(self.root.resolve()) if cwd is None else cwd}
        records = [session, *events] if include_session else events
        path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records))
        return path

    def mcp_event(self, identifier, arguments, reply=None, *, status="completed", update_kind="tool_call"):
        update = {"sessionUpdate": update_kind, "toolCallId": identifier, "kind": "execute", "status": status,
                  "rawInput": {"server": "clarity", "tool": "read_file", "arguments": arguments}}
        if reply is not None:
            update["rawOutput"] = {"result": {"content": [{"type": "text", "text": json.dumps(reply, ensure_ascii=False)}],
                                                "structuredContent": reply, "isError": False}, "error": None}
        elif status == "failed":
            update["rawOutput"] = {"result": None, "error": {"message": "synthetic timeout"}}
        return {"type": "session/update", "session_id": "test-session", "category": "tool", "update": update}

    def native_event(self, identifier, path, text, *, kind="read"):
        block = {"type": "text", "text": text}
        return {"type": "session/update", "session_id": "test-session", "category": "tool", "update": {
            "sessionUpdate": "tool_call", "toolCallId": identifier, "kind": kind, "status": "completed",
            "title": "Localized tool display", "rawInput": {"path": path},
            "content": [{"type": "content", "content": block}], "rawOutput": {"content": [block]}}}

    def test_materialization_preserves_frozen_text_identity_and_input(self):
        text = "\ufeff身份 = 'é'\r\n\r\nlast line"
        context = {"current": {"有 空格/run.py": frozen(text, "git-object", "100755")},
                   "baseline": {"removed.py": frozen("old\n", "old-object")}}
        original = copy.deepcopy(context)
        before_mode = stat.S_IMODE(self.base.stat().st_mode)
        manifest = materialize_context(context, self.root)
        self.assertEqual(context, original)
        path = self.root / "current/有 空格/run.py"
        self.assertEqual(path.read_bytes(), text.encode("utf-8"))
        record = manifest["current_files"]["有 空格/run.py"]
        self.assertEqual({key: record[key] for key in ("view_path", "oid", "mode", "lines", "bytes")},
                         {"view_path": "current/有 空格/run.py", "oid": "git-object", "mode": "100755",
                          "lines": 3, "bytes": len(text.encode("utf-8"))})
        self.assertEqual(json.loads((self.root / "context.json").read_text()), manifest)
        self.assertGreater(len((self.root / "context.json").read_text().splitlines()), 10)
        for folder, directories, files in os.walk(self.root):
            self.assertEqual(stat.S_IMODE(Path(folder).stat().st_mode), 0o555)
            for name in files:
                self.assertEqual(stat.S_IMODE((Path(folder) / name).stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), before_mode)

    def test_manifest_scope_and_explicit_versions_include_ignored_paths(self):
        store = self.store({".gitignore": frozen("ignored/\n"), "ignored/tracked.py": frozen("CURRENT\n"),
                            "new.py": frozen("new")}, {"removed.py": frozen("BASELINE\n")})
        self.assertEqual([record["path"] for record in store.list_files()["files"]],
                         [".gitignore", "ignored/tracked.py", "new.py"])
        self.assertEqual(store.read_file("ignored/tracked.py")["text"], "CURRENT\n")
        self.assertEqual(store.search("CURRENT")["matches"][0]["path"], "ignored/tracked.py")
        self.assertEqual(store.read_file("removed.py", version="baseline")["text"], "BASELINE\n")
        with self.assertRaisesRegex(ValueError, "Unknown context path"):
            store.read_file("removed.py")
        self.assertEqual(store.search("BASELINE")["total"], 0)

    def test_listing_has_stable_prefix_pagination_and_no_body(self):
        store = self.store({"src/b.py": frozen("b"), "other.py": frozen("outside prefix"), "src/a.py": frozen("a")})
        first = store.list_files(prefix="src/", limit=1)
        second = store.list_files(prefix="src/", offset=first["next_offset"], limit=1)
        self.assertEqual([first["files"][0]["path"], second["files"][0]["path"]], ["src/a.py", "src/b.py"])
        self.assertEqual(first["total"], 2)
        self.assertIsNone(second["next_offset"])
        self.assertNotIn("text", first["files"][0])
        self.assertEqual(store.list_files(offset=100)["files"], [])
        json.dumps(first)

    def test_unlisted_file_never_enters_queries(self):
        store = self.store({"known.py": frozen("known")})
        (self.root / "current").chmod(0o700)
        (self.root / "current/unlisted.py").write_text("HOST_SECRET_MARKER")
        with self.assertRaisesRegex(ValueError, "Unknown context path"):
            store.read_file("unlisted.py")
        self.assertEqual(store.search("HOST_SECRET_MARKER")["total"], 0)
        self.assertEqual(store.list_files()["total"], 1)

    def test_read_pages_reconstruct_long_unicode_lines_and_original_newlines(self):
        text = "界" * 20_001 + "\r\nsecond\nthird"
        store = self.store({"long.py": frozen(text)})
        pieces, cursor = [], {"start_line": 1, "start_column": 1}
        while cursor is not None:
            reply = store.read_file("long.py", **cursor, line_count=2, char_limit=701)
            self.assertLessEqual(len(reply["text"]), 701)
            pieces.append(reply["text"])
            cursor = reply["next"]
        self.assertEqual("".join(pieces), text)
        first = store.read_file("long.py", char_limit=16_000)
        self.assertEqual(first["next"], {"start_line": 1, "start_column": 16_001})
        self.assertTrue(first["truncated"])
        self.assertEqual(first["end_column"], 16_000)
        self.assertEqual(first["total_lines"], 3)

    def test_read_line_boundary_and_empty_file_are_explicit(self):
        store = self.store({"lines.py": frozen("one\ntwo\nthree\n"), "empty.py": frozen("")})
        first = store.read_file("lines.py", line_count=1)
        self.assertEqual(first["text"], "one\n")
        self.assertEqual(first["next"], {"start_line": 2, "start_column": 1})
        self.assertEqual(store.read_file("lines.py", start_line=2, start_column=2)["text"], "wo\nthree\n")
        self.assertEqual(store.read_file("lines.py", start_line=4)["text"], "")
        empty = store.read_file("empty.py")
        self.assertEqual((empty["text"], empty["total_lines"], empty["next"], empty["truncated"]), ("", 0, None, False))
        with self.assertRaises(ValueError):
            store.read_file("lines.py", start_column=6)
        with self.assertRaises(ValueError):
            store.read_file("lines.py", start_line=5)
        with self.assertRaises(ValueError):
            store.read_file("empty.py", start_column=2)

    def test_default_read_enforces_line_limit_with_a_continuation(self):
        store = self.store({"many.py": frozen("x\n" * 401)})
        reply = store.read_file("many.py")
        self.assertEqual(reply["text"], "x\n" * 200)
        self.assertEqual(reply["next"], {"start_line": 201, "start_column": 1})

    def test_numbered_text_labels_first_blank_and_last_original_lines(self):
        text = "首行\r\n\r\n末行"
        store = self.store(baseline={"original.py": frozen(text, "original-object", "100755")})
        reply = store.read_file("original.py", version="baseline")
        self.assertEqual(reply["numbered_text"], "1: 首行\r\n2: \r\n3: 末行")
        self.assertEqual({key: value for key, value in reply.items() if key != "numbered_text"}, {
            "version": "baseline", "path": "original.py", "oid": "original-object", "mode": "100755",
            "text": text, "start_line": 1, "start_column": 1, "end_line": 3, "end_column": 2,
            "next": None, "total_lines": 3, "truncated": False})
        self.assertEqual((self.root / "baseline/original.py").read_bytes(), text.encode("utf-8"))

    def test_numbered_text_pagination_keeps_absolute_line_numbers(self):
        store = self.store({"pages.py": frozen("first\nsecond\nthird\nfourth\nfifth\nlast")})
        first = store.read_file("pages.py", line_count=2)
        middle = store.read_file("pages.py", **first["next"], line_count=2)
        last = store.read_file("pages.py", **middle["next"], line_count=2)
        self.assertEqual(first["numbered_text"], "1: first\n2: second\n")
        self.assertEqual(middle["numbered_text"], "3: third\n4: fourth\n")
        self.assertEqual(last["numbered_text"], "5: fifth\n6: last")
        self.assertEqual(first["text"] + middle["text"] + last["text"], "first\nsecond\nthird\nfourth\nfifth\nlast")
        self.assertEqual(first["next"], {"start_line": 3, "start_column": 1})
        self.assertEqual(middle["next"], {"start_line": 5, "start_column": 1})
        self.assertIsNone(last["next"])

    def test_numbered_text_long_line_continuation_keeps_original_line_and_column(self):
        text = "界" * 20_001 + "\r\n尾"
        store = self.store({"long.py": frozen(text)})
        first = store.read_file("long.py")
        last = store.read_file("long.py", **first["next"])
        self.assertEqual(first["text"], "界" * 16_000)
        self.assertEqual(first["numbered_text"], "1: " + "界" * 16_000)
        self.assertEqual(first["next"], {"start_line": 1, "start_column": 16_001})
        self.assertEqual(last["start_line"], 1)
        self.assertEqual(last["start_column"], 16_001)
        self.assertEqual(last["numbered_text"], "1: " + "界" * 4_001 + "\r\n2: 尾")
        self.assertEqual(first["text"] + last["text"], text)
        self.assertIsNone(last["next"])

    def test_numbered_text_crlf_split_uses_existing_cursor_without_advancing_early(self):
        store = self.store({"crlf.py": frozen("甲\r\n乙")})
        first = store.read_file("crlf.py", char_limit=2)
        newline = store.read_file("crlf.py", **first["next"], char_limit=1)
        last = store.read_file("crlf.py", **newline["next"], char_limit=1)
        self.assertEqual((first["text"], first["numbered_text"], first["next"]),
                         ("甲\r", "1: 甲\r", {"start_line": 1, "start_column": 3}))
        self.assertEqual((newline["text"], newline["numbered_text"], newline["next"]),
                         ("\n", "1: \n", {"start_line": 2, "start_column": 1}))
        self.assertEqual((last["text"], last["numbered_text"], last["next"]), ("乙", "2: 乙", None))

    def test_numbered_text_is_empty_when_no_source_piece_is_returned(self):
        store = self.store({"empty.py": frozen(""), "one.py": frozen("one\n")})
        for path, cursor in (("empty.py", {}), ("one.py", {"start_line": 2}),
                             ("one.py", {"start_line": 1, "start_column": 5})):
            with self.subTest(path=path, cursor=cursor):
                reply = store.read_file(path, **cursor)
                self.assertEqual(reply["text"], "")
                self.assertEqual(reply["numbered_text"], "")
                self.assertIsNone(reply["next"])

    def test_numbered_text_uses_existing_splitlines_boundaries_and_read_line_limit(self):
        store = self.store({"separators.py": frozen("一\u2028二\v三\r四\n"), "many.py": frozen("x\n" * 401)})
        separators = store.read_file("separators.py")
        self.assertEqual(separators["numbered_text"], "1: 一\u20282: 二\v3: 三\r4: 四\n")
        self.assertEqual(separators["text"], "一\u2028二\v三\r四\n")
        self.assertEqual(separators["total_lines"], 4)
        page = store.read_file("many.py")
        self.assertTrue(page["numbered_text"].startswith("1: x\n"))
        self.assertTrue(page["numbered_text"].endswith("200: x\n"))
        self.assertNotIn("201: ", page["numbered_text"])
        self.assertEqual(page["text"], "x\n" * 200)
        self.assertEqual(page["next"], {"start_line": 201, "start_column": 1})

    def test_search_is_literal_and_pages_matching_lines(self):
        store = self.store({"b.py": frozen("a.*b repeated a.*b\n"), "a.py": frozen("a.*b\naxxb\na.*b\n")})
        first = store.search("a.*b", limit=1)
        second = store.search("a.*b", offset=first["next_offset"], limit=2)
        self.assertEqual(first["total"], 3)
        self.assertEqual([(match["path"], match["line"], match["column"]) for match in first["matches"] + second["matches"]],
                         [("a.py", 1, 1), ("a.py", 3, 1), ("b.py", 1, 1)])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(store.search("A.*B")["total"], 0)
        self.assertEqual(store.search("a.*b", prefix="b")["total"], 1)

    def test_search_excerpts_reveal_late_matches_without_returning_long_lines(self):
        store = self.store({"long.py": frozen("x" * 1000 + "needle" + "y" * 1000)})
        match = store.search("needle")["matches"][0]
        self.assertEqual(match["column"], 1001)
        self.assertEqual(match["excerpt_start_column"], 941)
        self.assertEqual(match["match_length"], 6)
        self.assertEqual(len(match["text"]), EXCERPT_CHARS)
        self.assertIn("needle", match["text"])
        self.assertTrue(match["excerpt_truncated"])

    def test_bounds_and_types_are_rejected_instead_of_silently_clamped(self):
        store = self.store({"a.py": frozen("text")})
        for kwargs in ({"limit": 101}, {"limit": 0}, {"offset": -1}, {"limit": True}, {"offset": "0"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                store.list_files(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                store.search("text", **kwargs)
        for kwargs in ({"line_count": 201}, {"char_limit": 16_001}, {"char_limit": 0}, {"start_line": 0}, {"start_column": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                store.read_file("a.py", **kwargs)
        for query in ("", "x" * 1001, "one\ntwo", "text\n", None):
            with self.subTest(query=query), self.assertRaises(ValueError):
                store.search(query)
        for version in ("HEAD", "../current", None, []):
            with self.subTest(version=version), self.assertRaises(ValueError):
                store.list_files(version=version)

    def test_traversal_absolute_alias_and_unknown_paths_are_denied(self):
        store = self.store({"nested/a.py": frozen("inside")})
        for path in ("../outside", "/etc/passwd", "nested/../nested/a.py", "./nested/a.py", "nested//a.py",
                     "nested/a.py/", "current/nested/a.py", "C:/outside", "C:\\outside", "nested\\a.py", "", None):
            with self.subTest(path=path), self.assertRaises(ValueError):
                store.read_file(path)
        for prefix in ("../", "/", "nested/../", "C:/", "nested\\"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                store.list_files(prefix=prefix)
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                store.search("inside", prefix=prefix)

    def test_projected_symlink_hardlink_and_special_file_substitutes_are_denied(self):
        store = self.store({"nested/a.py": frozen("inside")})
        outside = self.base / "outside"
        outside.write_text("OUTSIDE_SECRET")
        path = self.root / "current/nested/a.py"
        path.parent.chmod(0o700)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            store.read_file("nested/a.py")
        path.unlink()
        os.link(outside, path)
        with self.assertRaisesRegex(ValueError, "ordinary projection"):
            store.read_file("nested/a.py")
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, "ordinary projection"):
            store.read_file("nested/a.py")
        self.assertEqual(outside.read_text(), "OUTSIDE_SECRET")

    def test_parent_directory_symlink_cannot_redirect_a_declared_path(self):
        store = self.store({"nested/a.py": frozen("inside")})
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "a.py").write_text("OUTSIDE_SECRET")
        folder = self.root / "current/nested"
        self.restore_permissions()
        (folder / "a.py").unlink()
        folder.rmdir()
        folder.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            store.read_file("nested/a.py")

    def test_same_size_projection_rewrite_is_not_accepted_as_frozen_text(self):
        store = self.store({"a.py": frozen("original")})
        path = self.root / "current/a.py"
        path.chmod(0o600)
        path.write_text("modified")
        with self.assertRaisesRegex(ValueError, "changed"):
            store.read_file("a.py")

    def test_unsafe_or_conflicting_input_is_rejected_before_projection(self):
        for files in ({"../escape": frozen("x")}, {"/absolute": frozen("x")}, {"link": frozen("x", mode="120000")},
                      {"a": frozen("x"), "a/child": frozen("y")}, {"missing-text": {"oid": "id", "mode": "100644"}}):
            with self.subTest(files=files), self.assertRaises(ValueError):
                materialize_context({"current": files}, self.root)
            self.assertFalse(self.root.exists())
        with self.assertRaises(ValueError):
            materialize_context({"unexpected": {}}, self.root)

    def test_nonempty_or_symlink_roots_are_not_reused(self):
        self.root.mkdir()
        existing = self.root / "existing"
        existing.write_text("preserve")
        with self.assertRaises(ValueError):
            materialize_context({"current": {}}, self.root)
        self.assertEqual(existing.read_text(), "preserve")
        existing.unlink()
        self.root.rmdir()
        self.root.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            materialize_context({"current": {}}, self.root)
        with self.assertRaises(ValueError):
            ContextStore(self.root)

    def test_manifest_path_escape_is_rejected(self):
        self.store({"a.py": frozen("inside")})
        path = self.root / "context.json"
        manifest = json.loads(path.read_text())
        manifest["current_files"]["a.py"]["view_path"] = "../../outside"
        path.chmod(0o600)
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "view path"):
            ContextStore(self.root)

    def test_optional_audit_is_outside_view_and_has_no_source_or_search_payload(self):
        query = "SECRET_QUERY_SYNTHETIC_MARKER"
        log = self.base / "audit.jsonl"
        store = self.store({"a.py": frozen(query + "\n")}, log_path=log)
        store.list_files()
        store.read_file("a.py")
        store.search(query)
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([row["tool"] for row in rows], ["list_files", "read_file", "search"])
        self.assertEqual(rows[-1]["query_chars"], len(query))
        self.assertEqual(rows[-1]["total"], 1)
        self.assertNotIn(query, log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        with self.assertRaisesRegex(ValueError, "outside"):
            ContextStore(self.root, self.root / "audit.jsonl")
        alias = self.base / "log-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            ContextStore(self.root, alias / "audit.jsonl")

    def test_caller_can_restore_permissions_and_remove_projection_after_use(self):
        store = self.store({"nested/a.py": frozen("code")})
        self.assertEqual(store.read_file("nested/a.py")["text"], "code")
        self.restore_permissions()
        for folder, directories, files in os.walk(self.root, topdown=False):
            for name in files:
                (Path(folder) / name).unlink()
            for name in directories:
                (Path(folder) / name).rmdir()
        self.root.rmdir()
        self.assertFalse(self.root.exists())

    def test_native_confirmation_requires_exact_full_text_and_logs_only_metadata(self):
        text = "\ufeff" + "界" * 17_001 + "\r\nSYNTHETIC_SOURCE_MARKER"
        context = {"current": {"source.py": frozen(text)}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        for observed in (text[:-1], text + "\n", text.replace("\r\n", "\n"), "x" + text[1:], None):
            with self.subTest(observed_type=type(observed).__name__):
                self.assertFalse(store.record_native_read("source.py", "current", observed))
                self.assertFalse(log.exists())
        self.assertTrue(store.record_native_read("source.py", "current", text))
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(rows, [{"tool": "read_file", "version": "current", "path": "source.py",
                                 "start_line": 1, "start_column": 1, "returned_chars": len(text),
                                 "truncated": False, "source": "native"}])
        self.assertNotIn("SYNTHETIC_SOURCE_MARKER", log.read_text())
        self.assertTrue(assess_delivery(context, log)["complete"])

    def test_native_partial_rejection_can_be_completed_by_explicit_read_pages(self):
        context = {"current": {"a.py": frozen("first\r\nsecond\n")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        self.assertFalse(store.record_native_read("a.py", "current", "first\r\n"))
        self.assertFalse(assess_delivery(context, log)["complete"])
        cursor = {"start_line": 1, "start_column": 1}
        while cursor is not None:
            cursor = store.read_file("a.py", **cursor, char_limit=3)["next"]
        receipt = assess_delivery(context, log)
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["current"], {"total": 1, "complete": 1, "missing": []})

    def test_native_confirmation_keeps_version_and_path_boundaries(self):
        log = self.base / "audit.jsonl"
        store = self.store({"a.py": frozen("new")}, {"a.py": frozen("old")}, log)
        for path, version in (("../a.py", "current"), ("unknown.py", "current"), ("a.py", "HEAD")):
            with self.subTest(path=path, version=version), self.assertRaises(ValueError):
                store.record_native_read(path, version, "new")
        self.assertFalse(store.record_native_read("a.py", "baseline", "new"))
        self.assertFalse(log.exists())
        self.assertTrue(store.record_native_read("a.py", "baseline", "old"))
        self.assertEqual(json.loads(log.read_text())["version"], "baseline")
        path = self.root / "current/a.py"
        path.chmod(0o600)
        path.write_text("bad")
        with self.assertRaisesRegex(ValueError, "changed"):
            store.record_native_read("a.py", "current", "bad")

    def test_delivery_merges_fragmented_overlapping_out_of_order_unicode_reads(self):
        context = {"current": {"a.py": frozen("αβ\r\nγδε\nlast")}, "baseline": {}}
        original = copy.deepcopy(context)
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        store.read_file("a.py", start_line=3, start_column=2, char_limit=3)
        store.read_file("a.py", start_line=1, start_column=3, char_limit=4)
        store.read_file("a.py", start_line=2, start_column=2, char_limit=4)
        store.read_file("a.py", start_line=1, start_column=1, char_limit=3)
        receipt = assess_delivery(context, log)
        self.assertTrue(receipt["complete"])
        self.assertTrue(receipt["audit_valid"])
        self.assertEqual(receipt["current"], {"total": 1, "complete": 1, "missing": []})
        self.assertEqual(receipt["baseline"], {"total": 0, "complete": 0, "missing": []})
        self.assertEqual(context, original)
        json.dumps(receipt)

    def test_delivery_reports_exact_gaps_without_double_counting_repeated_reads(self):
        context = {"current": {"a.py": frozen("abcdefghi")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        store.read_file("a.py", start_column=3, char_limit=2)
        store.read_file("a.py", start_column=7, char_limit=2)
        store.read_file("a.py", start_column=3, char_limit=2)
        receipt = assess_delivery(context, log)
        self.assertFalse(receipt["complete"])
        self.assertTrue(receipt["audit_valid"])
        self.assertEqual(receipt["current"], {"total": 1, "complete": 0, "missing": [{
            "path": "a.py", "total_chars": 9, "delivered_chars": 4,
            "missing_ranges": [{"start": 0, "end": 2}, {"start": 4, "end": 6}, {"start": 8, "end": 9}]}]})

    def test_delivery_requires_both_versions_and_discovery_does_not_count(self):
        context = {"current": {"a.py": frozen("new"), "b.py": frozen("other")},
                   "baseline": {"old.py": frozen("old")}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], context["baseline"], log)
        store.list_files()
        store.list_files(version="baseline")
        store.search("new")
        store.search("other")
        store.search("old", version="baseline")
        discovery = assess_delivery(context, log)
        self.assertTrue(discovery["audit_valid"])
        self.assertFalse(discovery["complete"])
        self.assertEqual(discovery["current"]["complete"], 0)
        self.assertEqual(discovery["baseline"]["complete"], 0)
        store.read_file("a.py")
        store.read_file("b.py")
        current_only = assess_delivery(context, log)
        self.assertEqual(current_only["current"]["complete"], 2)
        self.assertEqual(current_only["baseline"], {"total": 1, "complete": 0, "missing": [{
            "path": "old.py", "total_chars": 3, "delivered_chars": 0,
            "missing_ranges": [{"start": 0, "end": 3}]}]})
        self.assertFalse(current_only["complete"])
        store.read_file("old.py", version="baseline")
        self.assertTrue(assess_delivery(context, log)["complete"])

    def test_delivery_empty_files_are_known_but_missing_audit_still_fails_closed(self):
        context = {"current": {"empty.py": frozen("")}, "baseline": {"old-empty.py": frozen("")}}
        log = self.base / "audit.jsonl"
        missing = assess_delivery(context, log)
        self.assertFalse(missing["complete"])
        self.assertFalse(missing["audit_valid"])
        self.assertEqual(missing["current"], {"total": 1, "complete": 1, "missing": []})
        self.assertEqual(missing["baseline"], {"total": 1, "complete": 1, "missing": []})
        log.touch()
        self.assertTrue(assess_delivery(context, log)["complete"])
        self.assertTrue(assess_delivery({"current": {}, "baseline": {}}, log)["complete"])

    def test_delivery_successful_zero_character_read_does_not_complete_nonempty_file(self):
        context = {"current": {"a.py": frozen("one\nlast")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        self.assertEqual(store.read_file("a.py", start_column=5, line_count=1)["text"], "")
        receipt = assess_delivery(context, log)
        self.assertTrue(receipt["audit_valid"])
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["current"]["missing"][0]["delivered_chars"], 0)
        self.assertEqual(receipt["current"]["missing"][0]["missing_ranges"], [{"start": 0, "end": 8}])

    def test_delivery_malformed_audit_never_passes_even_after_complete_valid_reads(self):
        context = {"current": {"a.py": frozen("abc")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        store.read_file("a.py")
        valid = log.read_text()
        with log.open("a") as output:
            output.write("not-json\n")
        receipt = assess_delivery(context, log)
        self.assertFalse(receipt["complete"])
        self.assertFalse(receipt["audit_valid"])
        self.assertEqual(receipt["current"]["complete"], 1)
        self.assertEqual(receipt["errors"], [{"line": 2, "reason": "invalid_audit_record"}])
        log.write_text(valid + valid.rstrip("\n"))
        self.assertFalse(assess_delivery(context, log)["complete"])

    def test_delivery_invalid_record_fields_cannot_manufacture_full_coverage(self):
        context = {"current": {"a.py": frozen("界\r\nz")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        good = {"tool": "read_file", "version": "current", "path": "a.py", "start_line": 1,
                "start_column": 1, "returned_chars": 4, "truncated": False}
        invalid = [[], {"tool": "unknown"}, {**good, "version": "HEAD"}, {**good, "path": "../a.py"},
                   {**good, "path": "unknown.py"}, {**good, "start_line": 0}, {**good, "start_line": 4},
                   {**good, "start_column": 0}, {**good, "start_column": 5}, {**good, "returned_chars": -1},
                   {**good, "returned_chars": True}, {**good, "returned_chars": 6}, {**good, "truncated": True},
                   {**good, "source": "unknown"}, {**good, "success": False},
                   {**good, "source": "native", "start_column": 2, "returned_chars": 3},
                   {key: value for key, value in good.items() if key != "returned_chars"},
                   {"tool": "list_files", "version": "current"}]
        for record in invalid:
            with self.subTest(record=record):
                log.write_text(json.dumps(record) + "\n")
                receipt = assess_delivery(context, log)
                self.assertFalse(receipt["complete"])
                self.assertFalse(receipt["audit_valid"])
                self.assertEqual(receipt["current"]["complete"], 0)
        log.write_text(json.dumps(good)[:-1] + ', "returned_chars": 4}\n')
        self.assertFalse(assess_delivery(context, log)["audit_valid"])
        log.write_text(json.dumps(good) + "\n")
        self.assertTrue(assess_delivery(context, log)["complete"])

    def test_delivery_invalid_encoding_symlink_hardlink_and_special_audits_fail_closed(self):
        context = {"current": {}, "baseline": {}}
        log = self.base / "audit.jsonl"
        log.write_bytes(b"\xff\n")
        self.assertFalse(assess_delivery(context, log)["complete"])
        log.unlink()
        outside = self.base / "outside.jsonl"
        outside.touch()
        log.symlink_to(outside)
        self.assertFalse(assess_delivery(context, log)["complete"])
        log.unlink()
        os.link(outside, log)
        self.assertFalse(assess_delivery(context, log)["complete"])
        log.unlink()
        os.mkfifo(log)
        self.assertFalse(assess_delivery(context, log)["complete"])
        log.unlink()
        log.mkdir()
        self.assertFalse(assess_delivery(context, log)["complete"])
        self.assertFalse(assess_delivery(context, None)["complete"])

    def test_delivery_invalid_context_does_not_pass_as_an_empty_scope(self):
        log = self.base / "audit.jsonl"
        log.touch()
        for context in (None, {"unknown": {}}, {"current": []}, {"current": {"a.py": {}}},
                        {"current": {"../outside": frozen("x")}}):
            with self.subTest(context=context):
                receipt = assess_delivery(context, log)
                self.assertFalse(receipt["complete"])
                self.assertEqual(receipt["errors"], [{"line": None, "reason": "invalid_context"}])

    def test_native_confirmation_does_not_report_success_when_audit_write_fails(self):
        store = self.store({"a.py": frozen("body")}, log_path=self.base)
        with self.assertRaises(OSError):
            store.record_native_read("a.py", "current", "body")

    def test_reply_feedback_accepts_only_whole_strict_schema_valid_json(self):
        log = self.base / "audit.jsonl"
        log.touch()
        feedback = make_reply_feedback({"current": {}, "baseline": {}}, log, REPLY_SCHEMA)
        self.assertIsNone(feedback('{"ok": true}'))
        self.assertIsNone(feedback(' \n{"ok": false}\t'))
        for raw in ('```json\n{"ok": true}\n```', 'Here: {"ok": true}', '{"ok": true} done',
                    '{"ok": true} {"ok": false}', '{"ok": true, "ok": false}', '{"ok": NaN}',
                    '{"ok": Infinity}', '{"ok": 1}', '{"ok": true, "extra": "value"}', '{}',
                    '[]', 'null', 'true', '', '{"ok":'):
            with self.subTest(raw=raw):
                message = feedback(raw)
                self.assertIsInstance(message, str)
                self.assertIn("The first reply is not an accepted complete review", message)
                self.assertIn("whole draft failed strict JSON/object/response-schema validation", message)
                self.assertIn("Source delivery is complete", message)
                self.assertIn("same Agent session", message)
                self.assertIn("without code fences or surrounding prose", message)

    def test_reply_feedback_does_not_embed_reply_schema_or_source_payload(self):
        context = {"current": {"source.py": frozen("SOURCE_SECRET_SYNTHETIC_MARKER")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        log.touch()
        schema = {**REPLY_SCHEMA, "description": "SCHEMA_SECRET_SYNTHETIC_MARKER"}
        before_context, before_schema = copy.deepcopy(context), copy.deepcopy(schema)
        message = make_reply_feedback(context, log, schema)("RAW_REPLY_SECRET_SYNTHETIC_MARKER")
        for marker in ("SOURCE_SECRET_SYNTHETIC_MARKER", "SCHEMA_SECRET_SYNTHETIC_MARKER", "RAW_REPLY_SECRET_SYNTHETIC_MARKER"):
            self.assertNotIn(marker, message)
        self.assertIn('version=current path="source.py" start_line=1 start_column=1', message)
        self.assertIn("line_count <= 200", message)
        self.assertIn("char_limit <= 16000", message)
        self.assertIn("list_files limit <= 100", message)
        self.assertIn("read_file (Codex MCP) or clarity_read_file (Pi)", message)
        self.assertEqual(context, before_context)
        self.assertEqual(schema, before_schema)
        self.assertEqual(log.read_text(), "")

    def test_reply_feedback_baseline_6834_character_prefix_points_to_original_line_201(self):
        prefix = ("界" * 32 + "\r\n") * 199 + "尾" * 66 + "\r\n"
        self.assertEqual(len(prefix), 6834)
        context = {"current": {}, "baseline": {"src/old.py": frozen(prefix + "目标尾部\n末")}}
        log = self.base / "audit.jsonl"
        store = self.store(baseline=context["baseline"], log_path=log)
        first = store.read_file("src/old.py", version="baseline")
        self.assertEqual(first["text"], prefix)
        self.assertEqual(first["next"], {"start_line": 201, "start_column": 1})
        feedback = make_reply_feedback(context, log, REPLY_SCHEMA)
        message = feedback('{"ok": true}')
        self.assertIn("1 missing ranges across 1 file versions", message)
        self.assertIn('version=baseline path="src/old.py" start_line=201 start_column=1', message)
        self.assertIn("end_line=203 end_column=1 missing_chars=6", message)
        self.assertNotIn("baseline/src/old.py", message)
        store.read_file("src/old.py", version="baseline", **first["next"])
        self.assertIsNone(feedback('{"ok": true}'))

    def test_reply_feedback_crlf_interior_cursor_advances_only_with_delivered_characters(self):
        context = {"current": {"crlf.py": frozen("甲\r\n乙")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        first = store.read_file("crlf.py", char_limit=2)
        feedback = make_reply_feedback(context, log, REPLY_SCHEMA)
        self.assertIn('path="crlf.py" start_line=1 start_column=3', feedback('{"ok": true}'))
        newline = store.read_file("crlf.py", **first["next"], char_limit=1)
        self.assertIn('path="crlf.py" start_line=2 start_column=1', feedback('{"ok": true}'))
        store.read_file("crlf.py", **newline["next"], char_limit=1)
        self.assertIsNone(feedback('{"ok": true}'))

    def test_reply_feedback_unicode_long_line_columns_are_not_byte_counts(self):
        context = {"current": {"long.py": frozen("界" * 20_000 + "尾")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        first = store.read_file("long.py")
        self.assertEqual(len(first["text"]), 16_000)
        self.assertEqual(len(first["text"].encode("utf-8")), 48_000)
        message = make_reply_feedback(context, log, REPLY_SCHEMA)('{"ok": true}')
        self.assertIn('path="long.py" start_line=1 start_column=16001', message)
        self.assertIn("end_line=2 end_column=1 missing_chars=4001", message)
        self.assertNotIn("界", message)

    def test_reply_feedback_keeps_existing_unicode_splitline_boundaries(self):
        context = {"current": {"separators.py": frozen("首\u2028二\v末")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        store.read_file("separators.py", char_limit=3)
        message = make_reply_feedback(context, log, REPLY_SCHEMA)('{"ok": true}')
        self.assertIn('path="separators.py" start_line=2 start_column=2', message)
        self.assertIn("missing_chars=2", message)

    def test_reply_feedback_limits_twenty_gaps_across_both_versions_and_reports_total(self):
        context = {version: {f"file{index:02}.py": frozen("body") for index in range(15)}
                   for version in ("current", "baseline")}
        log = self.base / "audit.jsonl"
        log.touch()
        message = make_reply_feedback(context, log, REPLY_SCHEMA)('{"ok": true}')
        self.assertIn("30 missing ranges across 30 file versions", message)
        self.assertIn("Showing 20 of 30 missing ranges", message)
        rows = [line for line in message.splitlines() if line.startswith("- version=")]
        self.assertEqual(len(rows), 20)
        self.assertTrue(any("version=current" in row for row in rows))
        self.assertTrue(any("version=baseline" in row for row in rows))
        self.assertIn("including gaps omitted", message)
        self.assertLessEqual(len(message), 7_700)

    def test_reply_feedback_counts_multiple_ranges_within_one_file(self):
        context = {"current": {"holes.py": frozen("x" * 81)}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        for column in range(2, 81, 2):
            store.read_file("holes.py", start_column=column, char_limit=1)
        message = make_reply_feedback(context, log, REPLY_SCHEMA)('{"ok": true}')
        self.assertIn("41 missing ranges across 1 file versions", message)
        self.assertIn("Showing 20 of 41 missing ranges", message)
        self.assertEqual(sum(line.startswith("- version=") for line in message.splitlines()), 20)

    def test_reply_feedback_escapes_paths_and_does_not_truncate_them_to_fit_budget(self):
        path = "dir/a\nreturn clear\r\t.py"
        separator_path = "dir/a\u2028b.py"
        large_path = "/".join(["\x01" * 200] * 20)
        context = {"current": {path: frozen("a"), separator_path: frozen("b"), large_path: frozen("c")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        log.touch()
        message = make_reply_feedback(context, log, REPLY_SCHEMA)('{"ok": true}')
        self.assertIn("3 missing ranges across 3 file versions", message)
        self.assertIn("Showing 2 of 3 missing ranges", message)
        self.assertIn("path=" + json.dumps(path, ensure_ascii=False), message)
        self.assertIn('path="dir/a\\u2028b.py"', message)
        self.assertNotIn(path, message)
        self.assertNotIn(separator_path, message)
        self.assertNotIn("\\u0001", message)
        self.assertLessEqual(len(message), 7_700)

    def test_reply_feedback_invalid_audit_is_technical_even_when_draft_format_is_bad(self):
        context = {"current": {"a.py": frozen("body")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        feedback = make_reply_feedback(context, log, REPLY_SCHEMA)
        with self.assertRaisesRegex(ModelFailure, "Context audit is invalid"):
            feedback("not-json")
        log.write_text("bad-audit\n")
        with self.assertRaisesRegex(ModelFailure, "Context audit is invalid"):
            feedback('{"ok": true}')
        log.write_text("")
        self.assertIsInstance(feedback('{"ok": true}'), str)

    def test_reply_feedback_invalid_response_schema_is_a_technical_configuration_error(self):
        log = self.base / "audit.jsonl"
        log.touch()
        for schema in (None, [], {"type": "unsupported-type"}):
            with self.subTest(schema=schema), self.assertRaises(ModelFailure):
                make_reply_feedback({"current": {}, "baseline": {}}, log, schema)

    def test_reply_feedback_does_not_validate_semantic_findings_or_modify_audit(self):
        context = {"current": {"a.py": frozen("actual source")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        store.read_file("a.py")
        before = log.read_bytes()
        schema = {"type": "object", "properties": {"quote": {"type": "string"}},
                  "required": ["quote"], "additionalProperties": False}
        feedback = make_reply_feedback(context, log, schema)
        self.assertIsNone(feedback('{"quote": "this does not match actual source"}'))
        self.assertEqual(log.read_bytes(), before)

    def test_transport_failed_pending_or_server_only_reads_do_not_complete_source(self):
        context = {"current": {"a.py": frozen("body")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        body = store.read_file("a.py")
        low_level = assess_delivery(context, log)
        self.assertTrue(low_level["complete"])
        self.assertFalse(low_level["transport_confirmed"])
        for status in ("failed", "pending", "in_progress"):
            with self.subTest(status=status):
                events = self.write_events([self.mcp_event("call", {"path": "a.py"}, body, status=status)])
                receipt = assess_delivery(context, log, events_path=events)
                self.assertTrue(receipt["audit_valid"])
                self.assertTrue(receipt["transport_confirmed"])
                self.assertFalse(receipt["complete"])
                self.assertEqual(receipt["current"]["missing"][0]["delivered_chars"], 0)
        receipt = assess_delivery(context, log, events_path=self.write_events([]))
        self.assertFalse(receipt["complete"])

    def test_transport_failed_timeout_then_successful_same_body_read_completes(self):
        context = {"current": {"a.py": frozen("body\r\n")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        store.read_file("a.py")
        failed = self.mcp_event("failed-call", {"path": "a.py"}, status="failed")
        failed_events = self.write_events([failed])
        self.assertFalse(assess_delivery(context, log, events_path=failed_events)["complete"])
        received = store.read_file("a.py")
        completed = self.mcp_event("retry-call", {"path": "a.py"}, received)
        receipt = assess_delivery(context, log, events_path=self.write_events([failed, completed]))
        self.assertTrue(receipt["complete"])
        self.assertTrue(receipt["transport_confirmed"])

    def test_transport_direct_completion_omitted_fields_and_duplicate_notifications(self):
        context = {"current": {"a.py": frozen("text")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        received = store.read_file("a.py")
        direct = self.mcp_event("call", {"path": "a.py"}, received)
        duplicate = {"type": "session/update", "session_id": "test-session", "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "call", "status": "completed", "title": "New display title"}}
        self.assertTrue(assess_delivery(context, log, events_path=self.write_events([direct, duplicate, duplicate]))["complete"])
        initial = copy.deepcopy(direct)
        initial["update"]["status"] = "in_progress"
        self.assertTrue(assess_delivery(context, log, events_path=self.write_events([initial, duplicate]))["complete"])
        conflict = copy.deepcopy(duplicate)
        conflict["update"]["rawInput"] = {"path": "different.py"}
        receipt = assess_delivery(context, log, events_path=self.write_events([direct, conflict]))
        self.assertFalse(receipt["complete"])
        self.assertFalse(receipt["audit_valid"])

    def test_transport_partial_unicode_body_counts_only_exact_received_range(self):
        context = {"current": {"a.py": frozen("甲\r\n乙😀e\u0301")}, "baseline": {"a.py": frozen("old")}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], context["baseline"], log)
        arguments = {"path": "a.py", "char_limit": 2}
        first = store.read_file(**arguments)
        events = [self.mcp_event("first", arguments, first)]
        receipt = assess_delivery(context, log, events_path=self.write_events(events))
        self.assertEqual(receipt["current"]["missing"][0]["missing_ranges"], [{"start": 2, "end": 7}])
        self.assertEqual(receipt["baseline"]["complete"], 0)
        rest_arguments = {"path": "a.py", **first["next"]}
        rest = store.read_file(**rest_arguments)
        old_arguments = {"path": "a.py", "version": "baseline"}
        old = store.read_file(**old_arguments)
        events.extend([self.mcp_event("rest", rest_arguments, rest), self.mcp_event("old", old_arguments, old)])
        self.assertTrue(assess_delivery(context, log, events_path=self.write_events(events))["complete"])

    def test_transport_fake_source_identity_cursor_or_body_is_not_credited(self):
        context = {"current": {"a.py": frozen("body")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        body = store.read_file("a.py")
        for changes in ({"text": "fake"}, {"oid": "wrong"}, {"mode": "100755"}, {"start_line": True},
                        {"numbered_text": "1: fake"}, {"end_column": 99}, {"next": {"start_line": 2, "start_column": 1}}):
            with self.subTest(changes=changes):
                events = self.write_events([self.mcp_event("call", {"path": "a.py"}, {**body, **changes})])
                receipt = assess_delivery(context, log, events_path=events)
                self.assertFalse(receipt["complete"])
                self.assertTrue(receipt["transport_confirmed"])
                self.assertEqual(receipt["current"]["complete"], 0)

    def test_transport_conflicting_content_copies_and_arbitrary_wrappers_fail_closed(self):
        context = {"current": {"a.py": frozen("body")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        body = store.read_file("a.py")
        event = self.mcp_event("call", {"path": "a.py"}, body)
        bad_structured = copy.deepcopy(event)
        bad_structured["update"]["rawOutput"]["result"]["structuredContent"]["text"] = "fake"
        bad_display = copy.deepcopy(event)
        bad_display["update"]["content"] = [{"type": "content", "content": {"type": "text", "text": "fake"}}]
        wrapped = copy.deepcopy(event)
        wrapped["update"]["rawOutput"] = {"nested": event["update"]["rawOutput"]}
        for value in (bad_structured, bad_display):
            with self.subTest(value=value):
                receipt = assess_delivery(context, log, events_path=self.write_events([value]))
                self.assertFalse(receipt["complete"])
                self.assertFalse(receipt["transport_confirmed"])
                self.assertFalse(receipt["audit_valid"])
        receipt = assess_delivery(context, log, events_path=self.write_events([wrapped]))
        self.assertFalse(receipt["complete"])
        self.assertTrue(receipt["audit_valid"])
        self.assertEqual(receipt["current"]["complete"], 0)

    def test_transport_empty_display_uses_received_mcp_body_and_absent_body_is_a_gap(self):
        context = {"current": {"a.py": frozen("body")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        body = store.read_file("a.py")
        event = self.mcp_event("call", {"path": "a.py"}, body)
        event["update"]["content"] = []
        self.assertTrue(assess_delivery(context, log, events_path=self.write_events([event]))["complete"])
        event["update"]["rawOutput"] = {"result": {"content": []}, "error": None}
        receipt = assess_delivery(context, log, events_path=self.write_events([event]))
        self.assertFalse(receipt["complete"])
        self.assertTrue(receipt["audit_valid"])
        self.assertTrue(receipt["transport_confirmed"])

    def test_transport_wrong_session_orphan_conflict_and_missing_events_fail_closed(self):
        context = {"current": {"a.py": frozen("body")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        event = self.mcp_event("call", {"path": "a.py"}, store.read_file("a.py"))
        foreign = {**event, "session_id": "foreign-session"}
        orphan = copy.deepcopy(event)
        orphan["update"]["sessionUpdate"] = "tool_call_update"
        failed = copy.deepcopy(event)
        failed["update"]["status"] = "failed"
        for records, include_session in (([foreign], True), ([orphan], True), ([event], False),
                                         ([failed, event], True), ([{"type": "session/new", "session": {"sessionId": "second"}}], True)):
            with self.subTest(records=records):
                events = self.write_events(records, include_session=include_session)
                receipt = assess_delivery(context, log, events_path=events)
                self.assertFalse(receipt["complete"])
                self.assertFalse(receipt["audit_valid"])
                self.assertFalse(receipt["transport_confirmed"])
        events = self.base / "missing-events.jsonl"
        self.assertFalse(assess_delivery(context, log, events_path=events)["complete"])
        events.write_text("invalid-json\n")
        self.assertFalse(assess_delivery(context, log, events_path=events)["audit_valid"])
        events.write_text(json.dumps(event))
        self.assertFalse(assess_delivery(context, log, events_path=events)["audit_valid"])

    def test_transport_native_reads_bind_original_path_and_recorded_cwd_without_title(self):
        context = {"current": {"a.py": frozen("原文\r\n")}, "baseline": {"a.py": frozen("旧文\n")}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], context["baseline"], log)
        self.assertTrue(store.record_native_read("a.py", "current", "原文\r\n"))
        self.assertTrue(store.record_native_read("a.py", "baseline", "旧文\n"))
        events = [self.native_event("current", "current/a.py", "原文\r\n"),
                  self.native_event("old", str(self.root.resolve() / "baseline/a.py"), "旧文\n"),
                  self.native_event("manifest", "context.json", (self.root / "context.json").read_text())]
        receipt = assess_delivery(context, log, events_path=self.write_events(events))
        self.assertTrue(receipt["complete"])
        self.assertTrue(receipt["transport_confirmed"])
        absolute = events[1]
        receipt = assess_delivery(context, log, events_path=self.write_events([absolute], cwd="/different/view"))
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["baseline"]["complete"], 0)
        path = self.write_events([events[0]])
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records[0].pop("cwd")
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        self.assertFalse(assess_delivery(context, log, events_path=path)["audit_valid"])

    def test_transport_native_partial_normalized_wrong_path_or_wrong_kind_does_not_count(self):
        context = {"current": {"a.py": frozen("\ufeff甲\r\n乙")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        store.record_native_read("a.py", "current", context["current"]["a.py"]["text"])
        cases = [("current/a.py", "\ufeff甲\n乙", "read"), ("current/a.py", "\ufeff甲", "read"),
                 ("current/a.py", "甲\r\n乙", "read"), ("current/unknown.py", "\ufeff甲\r\n乙", "read"),
                 ("./current/a.py", "\ufeff甲\r\n乙", "read"), ("/outside/a.py", "\ufeff甲\r\n乙", "read"),
                 ("current/a.py", "\ufeff甲\r\n乙", "other")]
        for path, text, kind in cases:
            with self.subTest(path=path, text=text, kind=kind):
                event = self.native_event("call", path, text, kind=kind)
                receipt = assess_delivery(context, log, events_path=self.write_events([event]))
                self.assertFalse(receipt["complete"])
                self.assertEqual(receipt["current"]["complete"], 0)

    def test_transport_own_fallback_uses_standard_text_and_exact_response_shape(self):
        context = {"current": {"a.py": frozen("界\r\n尾")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        first_args = {"path": "a.py", "char_limit": 2}
        first = store.read_file(**first_args)
        last_args = {"path": "a.py", **first["next"]}
        last = store.read_file(**last_args)
        events = []
        for identifier, arguments, body in (("first", first_args, first), ("last", last_args, last)):
            event = self.native_event(identifier, arguments["path"], json.dumps(body, ensure_ascii=False), kind="other")
            event["update"]["rawInput"] = arguments
            event["update"]["rawOutput"]["details"] = body
            events.append(event)
        self.assertTrue(assess_delivery(context, log, events_path=self.write_events(events))["complete"])

    def test_transport_discovery_and_received_body_without_server_audit_never_pass(self):
        context = {"current": {"a.py": frozen("body")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        body = store.read_file("a.py")
        for tool in ("list_files", "search"):
            event = self.mcp_event("call", {"path": "a.py"}, body)
            event["update"]["rawInput"]["tool"] = tool
            self.assertFalse(assess_delivery(context, log, events_path=self.write_events([event]))["complete"])
        log.write_text("")
        event = self.mcp_event("call", {"path": "a.py"}, body)
        receipt = assess_delivery(context, log, events_path=self.write_events([event]))
        self.assertFalse(receipt["complete"])
        self.assertFalse(receipt["audit_valid"])

    def test_reply_feedback_fills_transport_gaps_and_rechecks_live_events(self):
        context = {"current": {"a.py": frozen("body")}, "baseline": {}}
        log = self.base / "audit.jsonl"
        store = self.store(context["current"], log_path=log)
        store.read_file("a.py")
        failed = self.mcp_event("failed", {"path": "a.py"}, status="failed")
        events = self.write_events([failed])
        feedback = make_reply_feedback(context, log, REPLY_SCHEMA, events_path=events)
        message = feedback('{"ok": true}')
        self.assertIn('path="a.py" start_line=1 start_column=1', message)
        received = store.read_file("a.py")
        self.write_events([failed, self.mcp_event("completed", {"path": "a.py"}, received)])
        self.assertIsNone(feedback('{"ok": true}'))
        events.write_text("bad-event\n")
        with self.assertRaises(ModelFailure):
            feedback('{"ok": true}')


if __name__ == "__main__":
    unittest.main()
