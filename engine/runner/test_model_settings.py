"""test_model_settings.py 核对已选 Agent 的保持、自定义 ACP 入口和依赖启动边界。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import acp_runtime
from model import ModelClient, agent_settings
from model_common import ModelFailure, _CONTEXT_INSTRUCTIONS, _INSTRUCTIONS


class ModelSettingsTests(unittest.TestCase):
    def test_custom_acp_command_does_not_require_pi_or_codex(self):
        config = {"agent_command": ["team-review-agent", "--acp"]}
        with patch.dict(os.environ, {}, clear=True), \
                patch("model.shutil.which", side_effect=lambda name: "/team/bin/review-agent" if name == "team-review-agent" else None):
            selected = agent_settings(config)
        self.assertEqual(selected["agent"], "custom")
        self.assertEqual(selected["agent_command"], ["/team/bin/review-agent", "--acp"])
        self.assertEqual(config, {"agent_command": ["team-review-agent", "--acp"]})

    def test_explicit_agent_switch_does_not_launch_the_previous_binary(self):
        prior = {"agent": "codex", "agent_bin": "/old/codex", "codex_bin": "/old/codex",
                 "agent_command": ["/old/codex-acp"]}
        available = {"pi": "/team/bin/pi", "pi-acp": "/team/bin/pi-acp", "/old/codex": "/old/codex"}
        with patch.dict(os.environ, {"CLARITY_AGENT": "pi"}, clear=True), \
                patch("model.shutil.which", side_effect=available.get):
            selected = agent_settings(prior, from_environment=True)
        self.assertEqual(selected["agent"], "pi")
        self.assertEqual(selected["agent_bin"], "/team/bin/pi")
        self.assertEqual(selected["agent_command"], ["/team/bin/pi-acp"])
        self.assertEqual(prior["agent_bin"], "/old/codex")

    def test_existing_selection_ignores_another_sessions_environment(self):
        saved = {"agent": "custom", "agent_command": ["/team/agent", "--acp"], "model": "owner-model"}
        environment = {"CLARITY_AGENT": "pi", "CLARITY_AGENT_COMMAND": '["/other/agent"]'}
        with patch.dict(os.environ, environment, clear=True), \
                patch("model.shutil.which", side_effect=lambda name: name if name == "/team/agent" else None):
            selected = agent_settings(saved)
        self.assertEqual(selected, saved)

    def test_legacy_codex_binary_still_selects_the_common_acp_adapter(self):
        available = {"/owner/codex": "/owner/codex", "codex-acp": "/team/codex-acp"}
        with patch.dict(os.environ, {}, clear=True), patch("model.shutil.which", side_effect=available.get):
            selected = agent_settings({"codex_bin": "/owner/codex", "model": "owner-model"})
        self.assertEqual(selected["agent"], "codex")
        self.assertEqual(selected["agent_bin"], "/owner/codex")
        self.assertEqual(selected["agent_command"], ["/team/codex-acp"])
        self.assertEqual(selected["model"], "owner-model")

    def test_incomplete_cached_runtime_stops_before_reexecuting_itself(self):
        with tempfile.TemporaryDirectory(prefix="clarity-cache-test-") as temporary:
            cache = Path(temporary)
            key = f"acp-1.0.0-jsonschema-4.26.0-pathspec-1.1.1-mcp-1.30.0-py{sys.version_info.major}.{sys.version_info.minor}"
            cached_python = cache / "clarity" / key / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            cached_python.parent.mkdir(parents=True)
            cached_python.write_text("incomplete cached environment")
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}, clear=True), \
                    patch("acp_runtime.ready", return_value=False), \
                    patch("acp_runtime.sys.executable", str(cached_python)), \
                    patch("acp_runtime.os.execv") as execute:
                with self.assertRaisesRegex(RuntimeError, "Cached ACP dependencies are incomplete"):
                    acp_runtime.ensure_runtime()
            execute.assert_not_called()

    def test_context_codex_uses_projected_view_and_readonly_mcp_without_enabling_shell(self):
        """test_context_codex_uses_projected_view_and_readonly_mcp_without_enabling_shell() 核对取材方式改变而规则隔离保持。"""
        context = {"current": {"pkg/value.py": {"text": "value = 1\n", "oid": "frozen", "mode": "100644"}},
                   "baseline": {}}
        feedback = lambda text: None
        observed = {}

        def materialize(provided, root):
            observed["context"] = provided
            (root / "current/pkg").mkdir(parents=True)
            (root / "current/pkg/value.py").write_text(provided["current"]["pkg/value.py"]["text"])
            (root / "context.json").write_text("{}\n")
            for path in root.rglob("*"):
                path.chmod(0o555 if path.is_dir() else 0o444)
            root.chmod(0o555)
            return {}

        def inspect(*args):
            options = adapter.call_args.kwargs
            view = options["cwd"]
            observed["view"] = view
            self.assertEqual(view.name, "view")
            self.assertEqual((view / "current/pkg/value.py").read_text(), "value = 1\n")
            self.assertEqual((view / "current/pkg/value.py").stat().st_mode & 0o777, 0o444)
            self.assertTrue(options["allow_tools"])
            self.assertIs(options["reply_feedback"], feedback)
            self.assertEqual(options["instructions"], _CONTEXT_INSTRUCTIONS)
            self.assertEqual(options["max_request_chars"], 1_048_576)
            self.assertIsNone(options["startup_metadata_path"])
            self.assertEqual(options["environment"]["CLARITY_CONTEXT_ROOT"], str(view))
            configuration = json.loads(options["environment"]["CODEX_CONFIG"])
            self.assertEqual(configuration["sandbox_mode"], "read-only")
            self.assertFalse(configuration["features"]["shell_tool"])
            self.assertFalse(configuration["features"]["unified_exec"])
            self.assertEqual(configuration["project_doc_max_bytes"], 0)
            self.assertEqual(Path(configuration["model_instructions_file"]).read_text(), _CONTEXT_INSTRUCTIONS)
            self.assertEqual(len(options["mcp_servers"]), 1)
            server = options["mcp_servers"][0]
            self.assertEqual(server["command"], sys.executable)
            self.assertEqual(Path(server["args"][0]).name, "context_server.py")
            self.assertEqual(server["args"][1:3], ["--root", str(view)])
            self.assertEqual(server["env"], [])
            return {"accepted": True}

        with tempfile.TemporaryDirectory(prefix="clarity-model-context-test-") as temporary, \
                patch.dict(os.environ, {"CODEX_HOME": str(Path(temporary) / "owner-config")}, clear=True), \
                patch.dict(sys.modules, {"review_context": SimpleNamespace(materialize_context=materialize)}), \
                patch("acp_model.AcpModel") as adapter:
            adapter.return_value.run.side_effect = inspect
            result = ModelClient({"agent": "codex", "agent_command": ["adapter"], "codex_bin": "owner-codex"}).run(
                "prompt", {}, Path(temporary) / "stage/attempt", context=context, reply_feedback=feedback)
        self.assertEqual(result, {"accepted": True})
        self.assertIs(observed["context"], context)
        self.assertFalse(observed["view"].exists())

    def test_pi_context_does_not_force_a_provider_and_cleans_view_after_failure(self):
        """test_pi_context_does_not_force_a_provider_and_cleans_view_after_failure() 保留 Pi 的模型选择并回收失败回合。"""
        observed = {}

        def materialize(context, root):
            (root / "context.json").write_text("{}\n")
            (root / "context.json").chmod(0o444)
            root.chmod(0o555)
            return {}

        def fail(*args):
            options = adapter.call_args.kwargs
            observed["view"] = options["cwd"]
            self.assertTrue(options["allow_tools"])
            self.assertIsNone(options["mcp_servers"])
            self.assertIsNone(options["max_request_chars"])
            self.assertEqual(options["startup_metadata_path"], ("piAcp", "startupInfo"))
            environment = options["environment"]
            self.assertEqual(environment["CLARITY_PI_BINARY"], "owner-pi")
            self.assertEqual(environment["CLARITY_CONTEXT_PYTHON"], sys.executable)
            self.assertEqual(Path(environment["CLARITY_CONTEXT_LOG"]).name, "context-access.jsonl")
            self.assertEqual(Path(environment["PI_ACP_PI_COMMAND"]).name, "pi_entry.py")
            self.assertNotIn("CODEX_CONFIG", environment)
            self.assertEqual(options["instructions"], _CONTEXT_INSTRUCTIONS)
            raise RuntimeError("synthetic failed stage")

        with tempfile.TemporaryDirectory(prefix="clarity-model-context-test-") as temporary, \
                patch.dict(os.environ, {}, clear=True), \
                patch.dict(sys.modules, {"review_context": SimpleNamespace(materialize_context=materialize)}), \
                patch("acp_model.AcpModel") as adapter:
            adapter.return_value.run.side_effect = fail
            with self.assertRaisesRegex(RuntimeError, "synthetic failed stage"):
                ModelClient({"agent": "pi", "agent_command": ["adapter"], "agent_bin": "owner-pi"}).run(
                    "prompt", {}, Path(temporary) / "stage/attempt", context={"current": {}, "baseline": {}})
        self.assertFalse(observed["view"].exists())

    def test_closed_material_stage_keeps_no_context_tools_or_context_instruction(self):
        """test_closed_material_stage_keeps_no_context_tools_or_context_instruction() 防止代码取材能力泄露到盲判材料阶段。"""
        for agent in ("pi", "custom"):
            with self.subTest(agent=agent), patch.dict(os.environ, {}, clear=True), \
                    patch("acp_model.AcpModel") as adapter:
                adapter.return_value.run.return_value = {"accepted": True}
                result = ModelClient({"agent": agent, "agent_command": ["adapter"]}).run(
                    "prompt", {}, Path("stage/attempt"))
                options = adapter.call_args.kwargs
            self.assertEqual(result, {"accepted": True})
            self.assertFalse(options["allow_tools"])
            self.assertIsNone(options["reply_feedback"])
            self.assertIsNone(options["cwd"])
            self.assertIsNone(options["mcp_servers"])
            self.assertEqual(options["instructions"], _INSTRUCTIONS)
            self.assertEqual(options["startup_metadata_path"], ("piAcp", "startupInfo") if agent == "pi" else None)
            self.assertNotIn("CLARITY_CONTEXT_ROOT", options["environment"])

    def test_feedback_without_code_context_is_rejected_before_starting_agent(self):
        with patch("model.tempfile.TemporaryDirectory") as temporary, patch("acp_model.AcpModel") as adapter:
            with self.assertRaisesRegex(ModelFailure, "requires a frozen code context"):
                ModelClient({"agent": "pi", "agent_command": ["adapter"]}).run(
                    "prompt", {}, Path("stage/attempt"), reply_feedback=lambda text: "Retry")
        temporary.assert_not_called()
        adapter.assert_not_called()

    def test_custom_context_is_rejected_before_projection_or_agent_invocation(self):
        """test_custom_context_is_rejected_before_projection_or_agent_invocation() 将新能力严格限制到获准的两个 Agent。"""
        with patch("model.tempfile.TemporaryDirectory") as temporary, \
                patch("acp_model.AcpModel") as adapter, \
                patch.dict(sys.modules, {"review_context": SimpleNamespace(
                    materialize_context=lambda *args: self.fail("Unsupported context must not be projected"))}):
            with self.assertRaisesRegex(ModelFailure, "only for Pi and Codex; the stage is incomplete"):
                ModelClient({"agent": "custom", "agent_command": ["adapter"]}).run(
                    "prompt", {}, Path("stage/attempt"), context={"current": {}, "baseline": {}})
        temporary.assert_not_called()
        adapter.assert_not_called()

    def test_pi_entry_selects_explicit_readonly_tools_only_for_context_stages(self):
        """test_pi_entry_selects_explicit_readonly_tools_only_for_context_stages() 核对显式守卫与禁止自动项目指令并存。"""
        import pi_entry

        for has_context in (False, True):
            environment = {"CLARITY_PI_BINARY": "/owner/pi"}
            if has_context:
                environment["CLARITY_CONTEXT_ROOT"] = "/frozen/context"
            with self.subTest(has_context=has_context), patch.dict(os.environ, environment, clear=True), \
                    patch.object(sys, "argv", ["pi_entry.py", "--mode", "rpc"]), \
                    patch("pi_entry.os.execvpe") as execute:
                pi_entry.main()
                binary, argv, exported = execute.call_args.args
            self.assertEqual(binary, "/owner/pi")
            self.assertIn("--no-context-files", argv)
            self.assertIn("--no-extensions", argv)
            self.assertIn("--no-approve", argv)
            self.assertIn("--no-session", argv)
            self.assertNotIn("--model", argv)
            self.assertNotIn("--provider", argv)
            if has_context:
                self.assertNotIn("--no-tools", argv)
                self.assertEqual(argv[argv.index("--tools") + 1], "read,grep,find,ls,clarity_read_file")
                self.assertEqual(Path(argv[argv.index("--extension") + 1]).name, "pi_context_guard.mjs")
                self.assertEqual(argv[argv.index("--system-prompt") + 1], _CONTEXT_INSTRUCTIONS)
            else:
                self.assertIn("--no-tools", argv)
                self.assertNotIn("--extension", argv)
                self.assertEqual(argv[argv.index("--system-prompt") + 1], _INSTRUCTIONS)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the native Pi extension")
    def test_pi_guard_blocks_path_escape_symlinks_and_write_tools_before_execution(self):
        """test_pi_guard_blocks_path_escape_symlinks_and_write_tools_before_execution() 核对原生工具的路径身份及执行前边界。"""
        with tempfile.TemporaryDirectory(prefix="clarity-pi-guard-test-") as temporary:
            folder = Path(temporary)
            view = folder / "view"
            (view / "src").mkdir(parents=True)
            (view / "src/value.py").write_text("value = 1\n")
            (view / "src/a\u00a0b.py").write_text("FROZEN_NBSP_FILE\n")
            (view / "src/a b.py").write_text("DIFFERENT_ASCII_FILE\n")
            (folder / "outside.txt").write_text("OUTSIDE_MUST_NOT_ENTER_REVIEW\n")
            (view / "linked.txt").symlink_to(folder / "outside.txt")
            module = Path(__file__).with_name("pi_context_guard.mjs").as_uri()
            script = '''import guard from MODULE;
import { readFileSync, realpathSync } from "node:fs";
import { sep } from "node:path";
let handler;
guard({registerTool:()=>{},on:(event, fn)=>{if(event === "tool_call") handler=fn;}});
const root=process.env.CLARITY_CONTEXT_ROOT;
const canonical=realpathSync(root);
const cases=[
 ["read",{path:"src/value.py"},false],
 ["read",{path:"@src/value.py"},false],
 ["read",{path:"../outside.txt"},true],
 ["read",{path:"linked.txt"},true],
 ["read",{path:new URL("../outside.txt",`file://${root}/`).href},true],
 ["grep",{path:"..",pattern:"OUTSIDE"},true],
 ["write",{path:"src/value.py"},true],
 ["bash",{command:"pwd"},true],
 ["ls",{},false],
 ["read",{path:"missing.py"},true],
 ["read",{path:"src/a\\u00a0b.py"},true],
 ["read",{path:"@src/a\\u00a0b.py"},true],
 ["read",{path:new URL("src/a\\u00a0b.py",`file://${root}/`).href},true],
 ["read",{path:"src/a b.py"},false,"DIFFERENT_ASCII_FILE\\n"]
];
for(const [toolName,input,blocked,content] of cases){
 const result=await handler({toolName,input},{cwd:root});
 if(Boolean(result?.block)!==blocked)throw Error(`Unexpected scope decision: ${toolName} ${JSON.stringify(input)}`);
 if(!blocked && input.path!==canonical && !input.path.startsWith(canonical+sep))throw Error("Native path was not normalized");
 if(content!==undefined && readFileSync(input.path,"utf8")!==content)throw Error("Frozen file identity was changed");
}
process.stdout.write(JSON.stringify({checked:cases.length}));
'''.replace("MODULE", json.dumps(module))
            result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                                    env=dict(os.environ, CLARITY_CONTEXT_ROOT=str(view), CLARITY_CONTEXT_PYTHON=sys.executable),
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout), {"checked": 14})
            self.assertEqual((view / "src/value.py").read_text(), "value = 1\n")
            self.assertEqual((view / "src/a\u00a0b.py").read_text(), "FROZEN_NBSP_FILE\n")

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the native Pi extension")
    def test_pi_bounded_reader_reuses_original_paths_and_cursors_without_project_execution(self):
        """test_pi_bounded_reader_reuses_original_paths_and_cursors_without_project_execution() 核对长行续读、原文身份及清单边界。"""
        from model import _restore_view_permissions
        from review_context import materialize_context

        text = "A" * 90000 + "TAIL_SENTINEL" + "B" * 10000 + "\r\n😀汉é\r\nfinal"
        record = lambda value: {"text": value, "oid": "frozen", "mode": "100644"}
        context = {"current": {"long.txt": record(text), "a\u00a0b.py": record("FROZEN_NBSP_FILE\r\n"),
                               "a b.py": record("DIFFERENT_ASCII_FILE\r\n")},
                   "baseline": {"long.txt": record("OLD\r\n")}}
        with tempfile.TemporaryDirectory(prefix="clarity-pi-cursor-test-") as temporary:
            folder = Path(temporary)
            view = folder / "view"
            materialize_context(context, view)
            try:
                script = '''import guard from MODULE;
let tool,handler;
guard({registerTool:t=>{tool=t;},on:(event,fn)=>{if(event==="tool_call")handler=fn;}});
if(tool.name!=="clarity_read_file")throw Error("Bounded tool was not registered");
async function read(params){
 const original=JSON.stringify(params);
 const decision=await handler({toolName:tool.name,input:params},{cwd:process.env.CLARITY_CONTEXT_ROOT});
 if(decision?.block||JSON.stringify(params)!==original)throw Error("Original path was rewritten by native guard");
 try{
  const result=await tool.execute("test",params,undefined);
  return JSON.parse(result.content[0].text);
 }catch(error){return {error:error.message};}
}
let cursor={start_line:1,start_column:1},text="",pages=0;
while(cursor){
 const result=await read({path:"long.txt",version:"current",...cursor,char_limit:16000});
 if(result.error||Array.from(result.text).length>16000)throw Error("Unbounded or failed read");
 text+=result.text;cursor=result.next;pages++;
 if(pages>20)throw Error("Cursor did not advance");
}
if(text!=="A".repeat(90000)+"TAIL_SENTINEL"+"B".repeat(10000)+"\\r\\n😀汉é\\r\\nfinal")throw Error("Long/Unicode/CRLF text changed");
if((await read({path:"a\\u00a0b.py"})).text!=="FROZEN_NBSP_FILE\\r\\n")throw Error("NBSP identity changed");
if((await read({path:"a b.py"})).text!=="DIFFERENT_ASCII_FILE\\r\\n")throw Error("Space identity changed");
if((await read({path:"long.txt",version:"baseline"})).text!=="OLD\\r\\n")throw Error("Baseline mapping changed");
for(const path of ["../outside.txt",process.env.CLARITY_CONTEXT_ROOT+"/long.txt","current/long.txt"]){
 if(!(await read({path})).error)throw Error("Path escaped manifest lookup");
}
if(!(await read({path:"long.txt",char_limit:16001})).error)throw Error("Oversized page accepted");
process.stdout.write(JSON.stringify({pages,original_paths:true,unicode_crlf:true,outside_denied:true}));
'''.replace("MODULE", json.dumps(Path(__file__).with_name("pi_context_guard.mjs").as_uri()))
                result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                                        env=dict(os.environ, CLARITY_CONTEXT_ROOT=str(view),
                                                 CLARITY_CONTEXT_PYTHON=sys.executable,
                                                 CLARITY_CONTEXT_LOG=str(folder / "audit.jsonl")),
                                        text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), {"pages": 7, "original_paths": True,
                                                           "unicode_crlf": True, "outside_denied": True})
                self.assertEqual((view / "current/long.txt").read_bytes(), text.encode("utf-8"))
            finally:
                _restore_view_permissions(view)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the native Pi extension")
    def test_pi_native_read_audit_counts_only_guarded_exact_successful_file_delivery(self):
        """test_pi_native_read_audit_counts_only_guarded_exact_successful_file_delivery() 核对全文交付证据且保持原生结果原样。"""
        from model import _restore_view_permissions
        from review_context import materialize_context

        record = lambda text: {"text": text, "oid": "frozen", "mode": "100644"}
        context = {"current": {"src/value.py": record("first\r\n汉😀\r\nlast\r\n")},
                   "baseline": {"src/value.py": record("old\n")}}
        with tempfile.TemporaryDirectory(prefix="clarity-pi-native-audit-test-") as temporary:
            folder = Path(temporary)
            view = folder / "view"
            materialize_context(context, view)
            (folder / "outside.txt").write_text("OUTSIDE_MUST_NOT_COUNT\n")
            try:
                script = '''import guard from MODULE;
import { chmodSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
const handlers={};
guard({registerTool:()=>{},on:(event,fn)=>{handlers[event]=fn;}});
const root=process.env.CLARITY_CONTEXT_ROOT;
const current=readFileSync(join(root,"current/src/value.py"),"utf8");
const baseline=readFileSync(join(root,"baseline/src/value.py"),"utf8");
async function result(id,path,text,extra={}){
 const input={path};
 await handlers.tool_call({toolCallId:id,toolName:"read",input},{cwd:root});
 const event={toolCallId:id,toolName:"read",input,content:[{type:"text",text}],isError:false,...extra};
 const before=JSON.stringify(event);
 const returned=await handlers.tool_result(event,{cwd:root});
 if(returned!==undefined||JSON.stringify(event)!==before)throw Error("Audit changed a native result");
 return event;
}
await result("full","current/src/value.py",current);
await result("baseline","baseline/src/value.py",baseline);
await result("partial","current/src/value.py","first\\r\\n\\n[More lines remain]");
await result("truncated","current/src/value.py",current,{details:{truncation:{truncated:true}}});
await result("failed","current/src/value.py",current,{isError:true});
await result("unknown-status","current/src/value.py",current,{isError:undefined});
await result("nontext","current/src/value.py",current,{content:[{type:"image",data:"none",mimeType:"image/png"}]});
await result("outside","../outside.txt","OUTSIDE_MUST_NOT_COUNT\\n");
const unknown={toolCallId:"not-guarded",toolName:"read",input:{path:join(root,"current/src/value.py")},content:[{type:"text",text:current}],isError:false};
await handlers.tool_result(unknown,{cwd:root});
const input={path:"current/src/value.py"};
await handlers.tool_call({toolCallId:"tampered",toolName:"read",input},{cwd:root});
chmodSync(input.path,0o644);writeFileSync(input.path,"tampered\\n");
const event={toolCallId:"tampered",toolName:"read",input,content:[{type:"text",text:"tampered\\n"}],isError:false};
const original=JSON.stringify(event);let tamperRejected=false;
try{await handlers.tool_result(event,{cwd:root});}catch(error){tamperRejected=error.message.includes("changed");}
if(!tamperRejected||JSON.stringify(event)!==original)throw Error("Tampered source was counted or native body changed");
const rows=readFileSync(process.env.CLARITY_CONTEXT_LOG,"utf8").trim().split("\\n").map(JSON.parse);
if(rows.length!==2||rows.some(row=>row.tool!=="read_file"||row.source!=="native"||row.truncated!==false))throw Error("Incomplete or failed reads counted as complete");
if(rows[0].version!=="current"||rows[0].path!=="src/value.py"||rows[0].returned_chars!==Array.from(current).length)throw Error("Current mapping changed");
if(rows[1].version!=="baseline"||rows[1].path!=="src/value.py"||rows[1].returned_chars!==Array.from(baseline).length)throw Error("Baseline mapping changed");
process.stdout.write(JSON.stringify({native_entries:rows.length,tamper_rejected:true,tool_results_unchanged:true}));
'''.replace("MODULE", json.dumps(Path(__file__).with_name("pi_context_guard.mjs").as_uri()))
                result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                                        env=dict(os.environ, CLARITY_CONTEXT_ROOT=str(view),
                                                 CLARITY_CONTEXT_PYTHON=sys.executable,
                                                 CLARITY_CONTEXT_LOG=str(folder / "audit.jsonl")),
                                        text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), {"native_entries": 2, "tamper_rejected": True,
                                                           "tool_results_unchanged": True})
            finally:
                _restore_view_permissions(view)

    def test_deleted_sidecar_hook_returns_before_dependency_setup(self):
        import review

        with tempfile.TemporaryDirectory(prefix="clarity-deleted-hook-") as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            argv = ["review.py", "hook", "pre-commit", "--repo", str(repo)]
            with patch.object(sys, "argv", argv), \
                    patch("acp_runtime.ensure_runtime", side_effect=AssertionError("Deleted sidecar must not bootstrap")) as prepare:
                self.assertEqual(review.main(), 0)
            prepare.assert_not_called()
            self.assertFalse((repo / ".clarity").exists())


if __name__ == "__main__":
    unittest.main()
