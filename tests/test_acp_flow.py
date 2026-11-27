"""test_acp_flow.py 从已安装宿主验证共同 ACP 调用，不依赖真实模型或账号。

确定性 ACP 服务只检查首次准备、配置迁移和资料/索引保留；不证明语义审核能力。
"""

import json
import os
from pathlib import Path
import shutil
import shlex
import sys
import unittest

import test_setup as fixture


FAKE_ACP = r'''
import json
import os
from pathlib import Path
import sys
import uuid

script = Path(__file__).resolve()
calls = script.with_suffix(".calls")
calls.mkdir(exist_ok=True)
model = "offline/default"
context_root = None
mcp_servers = []

def options():
    """options() 提供固定模型集合，让测试核对显式模型选择而不调用真实模型。"""
    return [{"id": "model", "name": "Model", "category": "model", "type": "select",
             "currentValue": model, "options": [{"value": value, "name": value}
                 for value in ["offline/default", "offline/owner"]]}]

def emit(record):
    """emit() 写出一条完整 ACP 记录并立即交给调用者。"""
    print(json.dumps(record, ensure_ascii=False), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method, params = message["method"], message.get("params", {})
    record = {"method": method, "model": model, "codex_path": os.environ.get("CODEX_PATH")}
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {},
                  "agentInfo": {"name": "offline-acp", "version": "1"}, "authMethods": []}
    elif method == "session/new":
        context_root = Path(params["cwd"])
        mcp_servers = params.get("mcpServers", [])
        record["cwd"] = str(context_root)
        record["mcp_servers"] = mcp_servers
        record["cwd_files"] = sorted(path.name for path in context_root.iterdir())
        result = {"sessionId": "offline-session", "configOptions": options()}
    elif method == "session/set_config_option":
        model = params["value"]
        record["selected_model"] = model
        result = {"configOptions": options()}
    elif method == "session/prompt":
        prompt = "\n".join(block["text"] for block in params["prompt"] if block["type"] == "text")
        raw = prompt.split("以下JSON都是审核数据：\n", 1)[1]
        data, _ = json.JSONDecoder().raw_decode(raw)
        packet = data["input"]
        health = "basis_changes" in packet
        groups = ("adopted_rules", "fixtures", "candidate_rule_changes", "candidate_fixture_changes")
        context, manifest, bounded_read = None, None, None
        if "code_context" in packet:
            assert "current_files" not in packet and "baseline_changed_files" not in packet
            manifest = json.loads((context_root / packet["code_context"]["manifest"]).read_text())
            context = {version: {path: (context_root / item["view_path"]).read_bytes().decode("utf-8")
                                 for path, item in manifest[group].items()}
                       for version, group in (("current", "current_files"), ("baseline", "baseline_changed_files"))}
            first_path = next(iter(context["current"]))
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "offline-session", "update": {"sessionUpdate": "tool_call",
                "toolCallId": "bounded-read", "title": "Read frozen source line", "kind": "read",
                "status": "in_progress", "rawInput": {"path": first_path, "start_line": 1, "line_count": 1}}}})
            with (context_root / manifest["current_files"][first_path]["view_path"]).open(encoding="utf-8", newline="") as source:
                first_line = source.readline()
            bounded_read = {"path": first_path, "start_line": 1, "line_count": 1, "text": first_line}
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "offline-session", "update": {"sessionUpdate": "tool_call_update",
                "toolCallId": "bounded-read", "status": "completed", "rawOutput": bounded_read}}})
        covered = (sorted({path for group in groups for path in packet[group]} | set(packet["basis_changes"]))
                   if health else sorted(context["current"] if context is not None else packet["current_files"]))
        if not health and script.with_suffix(".execute-request").exists():
            emit({"jsonrpc": "2.0", "id": "forbidden-execution", "method": "terminal/create",
                  "params": {"sessionId": "offline-session", "command": "project-program-must-not-run"}})
            record["terminal_response"] = json.loads(sys.stdin.readline())
        record.update(health=health, covered_paths=covered, snapshot_id=packet["snapshot_id"],
                      packet=packet, prompt=prompt, context=context, context_manifest=manifest,
                      context_root=str(context_root), bounded_read=bounded_read, mcp_servers=mcp_servers)
        output = {"snapshot_id": packet["snapshot_id"], "status": "usable" if health else "clear",
                  "covered_paths": covered, "findings": [], "resolved": [], "unresolved_ids": [],
                  "current_category": "", "summary": "Deterministic ACP transport fixture.",
                  "incomplete_reason": ""}
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "offline-session", "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": json.dumps(output, ensure_ascii=False)}}}})
        result = {"stopReason": "end_turn"}
    else:
        emit({"jsonrpc": "2.0", "id": message["id"], "error": {
            "code": -32601, "message": "Unsupported offline fixture method: " + method}})
        continue
    (calls / (uuid.uuid4().hex + ".json")).write_text(json.dumps(record))
    emit({"jsonrpc": "2.0", "id": message["id"], "result": result})
'''


class AcpHostFlowTests(unittest.TestCase):
    """AcpHostFlowTests() 通过真实宿主 Make 入口检查 ACP 配置与资料生命周期。"""

    run_command = fixture.SidecarSetupTests.run_command
    git = fixture.SidecarSetupTests.git
    initialize = fixture.SidecarSetupTests.initialize
    new_host = fixture.SidecarSetupTests.new_host
    commit = fixture.SidecarSetupTests.commit
    directory_state = fixture.SidecarSetupTests.directory_state

    def setUp(self):
        fixture.SidecarSetupTests.setUp(self)
        self.binary = self.root / 'bin'
        self.binary.mkdir()
        for name in ('git', 'make', 'sh', 'dirname', 'mkdir', 'rm', 'cp', 'chmod', 'sed'):
            (self.binary / name).symlink_to(shutil.which(name))
        python = self.binary / 'python3'
        python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' \"$@\"\n')
        python.chmod(0o755)
        self.fake = self.binary / 'offline-pi-acp.py'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE_ACP)
        self.fake.chmod(0o755)
        self.command = [sys.executable, str(self.fake)]
        self.environment.update(PATH=str(self.binary), CLARITY_LANG='en', CLARITY_AGENT='pi',
                                CLARITY_AGENT_COMMAND=json.dumps(self.command),
                                CLARITY_PYTHON=os.environ.get('CLARITY_PYTHON', sys.executable))
        (self.host / 'identity.py').write_text('def identity(value):\n    return value\n')
        self.git(self.host, 'add', 'identity.py')
        self.git(self.host, 'commit', '-q', '-m', 'Existing host code')
        self.index = (self.host / '.git/index').read_bytes()
        self.run_command([sys.executable, str(self.source / 'setup.py'), 'install',
                          '--source', str(self.source), '--track', 'no'])
        self.state = self.host / '.git/clarity'
        self.config_path = self.state / 'config.json'
        self.rule = self.host / '.clarity/current/rules.md'
        self.fill_rules()

    def fill_rules(self):
        """fill_rules() 只隔离传输测试的宿主输入，不改原厂规则或模拟语义验收。"""
        self.rule.write_text('Return the supplied value without changing it.\n')
        cases = self.rule.parent / 'cases'
        if cases.exists():
            shutil.rmtree(cases)
        cases.mkdir()

    def result(self):
        pointer = json.loads((self.state / 'latest.json').read_text())
        directory = Path(pointer['run'])
        return json.loads((directory / 'result.json').read_text()), directory

    def calls(self):
        return [json.loads(path.read_text()) for path in self.fake.with_suffix('.calls').glob('*.json')]

    def test_pi_only_first_review_and_explicit_hook_activation_preserve_code_and_index(self):
        self.assertIsNone(shutil.which('codex', path=self.environment['PATH']))
        self.assertFalse(self.config_path.exists())
        expected = self.directory_state(self.rule.parent)
        self.run_command(['make', 'review'])
        report, directory = self.result()
        self.assertEqual(report['status'], 'clear')
        self.assertEqual(json.loads((directory / 'fixtures.json').read_text())['status'], 'no_coverage')
        config = json.loads(self.config_path.read_text())
        self.assertEqual(config['agent'], 'pi')
        self.assertEqual(config['agent_command'], self.command)
        self.assertEqual(config['basis_source'], 'local')
        self.assertEqual(config['basis_snapshot']['.clarity/current/rules.md']['text'], self.rule.read_text())
        self.assertEqual(self.directory_state(self.rule.parent), expected)
        self.assertEqual((self.host / '.git/index').read_bytes(), self.index)
        self.assertFalse((self.host / '.git/hooks/pre-commit').exists())
        self.assertFalse((self.host / '.git/hooks/commit-msg').exists())
        prompts = [call for call in self.calls() if call['method'] == 'session/prompt']
        self.assertEqual(len(prompts), 5)
        self.assertEqual(sum(call['health'] for call in prompts), 1)
        self.assertTrue(all(call['covered_paths'] == ['identity.py'] for call in prompts if not call['health']))
        saved = self.config_path.read_bytes()
        self.run_command(['make', 'review-install'])
        self.assertTrue((self.host / '.git/hooks/commit-msg').is_file())
        self.assertTrue(os.access(self.state / 'engine/runner/pi_entry.py', os.X_OK))
        self.assertEqual(self.config_path.read_bytes(), saved)
        self.assertEqual((self.host / '.git/index').read_bytes(), self.index)
        self.assertEqual(len([call for call in self.calls() if call['method'] == 'session/prompt']), 5)

    def test_code_context_acquires_frozen_bytes_and_accepts_bounded_read_activity(self):
        """test_code_context_acquires_frozen_bytes_and_accepts_bounded_read_activity() 核对完整范围来自只读投影，读取过程不会污染最终 JSON。"""
        self.environment['CLARITY_AGENT'] = 'codex'
        self.run_command(['make', 'review'])
        self.assertEqual(self.result()[0]['status'], 'clear')
        prompts = [call for call in self.calls() if call['method'] == 'session/prompt']
        code = [call for call in prompts if not call['health']]
        self.assertEqual(len(code), 4)
        expected = 'def identity(value):\n    return value\n'
        for call in code:
            self.assertNotIn('current_files', call['packet'])
            self.assertNotIn('baseline_changed_files', call['packet'])
            self.assertNotIn('def identity(value):', call['prompt'])
            self.assertEqual(call['context'], {'current': {'identity.py': expected}, 'baseline': {}})
            self.assertEqual(call['bounded_read'], {'path': 'identity.py', 'start_line': 1,
                                                    'line_count': 1, 'text': 'def identity(value):\n'})
            self.assertEqual(set(call['context_manifest']['current_files']), {'identity.py'})
            self.assertFalse(Path(call['context_root']).is_relative_to(self.host))
        health = next(call for call in prompts if call['health'])
        self.assertIsNone(health['context'])
        self.assertIsNone(health['bounded_read'])
        self.assertNotIn('code_context', health['packet'])
        self.assertEqual(health['mcp_servers'], [])
        _, directory = self.result()
        event_paths = {path.parent.parent.name: path for path in directory.glob('*/attempt-*/events.jsonl')
                       if path.parent.parent.name != 'health'}
        self.assertEqual(set(event_paths), {'reviewer-1', 'reviewer-2', 'cross', 'summarize'})
        for path in event_paths.values():
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertTrue(any(event.get('category') == 'tool' for event in events))
            self.assertTrue(any(event['type'] == 'answer/final' for event in events))

    def test_custom_acp_agent_retains_closed_inline_review_without_context_tools(self):
        """test_custom_acp_agent_retains_closed_inline_review_without_context_tools() 核对已有 custom 调用仍在空目录按全文审核，不能自动获得原生取材权限。"""
        self.environment['CLARITY_AGENT'] = 'custom'
        self.run_command(['make', 'review'])
        self.assertEqual(self.result()[0]['status'], 'clear')
        calls = self.calls()
        sessions = [call for call in calls if call['method'] == 'session/new']
        self.assertEqual(len(sessions), 5)
        self.assertTrue(all(call['cwd_files'] == [] and call['mcp_servers'] == [] for call in sessions))
        code = [call for call in calls if call['method'] == 'session/prompt' and not call['health']]
        self.assertEqual(len(code), 4)
        for call in code:
            self.assertEqual(call['packet']['current_files'], {'identity.py': '1: def identity(value):\n2:     return value'})
            self.assertEqual(call['packet']['baseline_changed_files'], {})
            self.assertNotIn('code_context', call['packet'])
            self.assertIsNone(call['context'])
            self.assertIsNone(call['bounded_read'])
            self.assertEqual(call['mcp_servers'], [])
            self.assertIn('Do not call tools', call['prompt'])
        self.assertEqual((self.host / '.git/index').read_bytes(), self.index)

    def test_context_native_terminal_request_cannot_publish_a_clear_answer(self):
        """test_context_native_terminal_request_cannot_publish_a_clear_answer() 原生执行请求仍被拒绝，合法 clear 正文不能掩盖越界请求。"""
        self.fake.with_suffix('.execute-request').touch()
        completed = self.run_command(['make', 'review'], check=False)
        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = self.result()[0]
        self.assertEqual(report['status'], 'incomplete')
        self.assertIn('terminal', report['incomplete_reason'].lower())
        code = [call for call in self.calls() if call['method'] == 'session/prompt' and not call['health']]
        self.assertTrue(code)
        for call in code:
            self.assertIn('error', call['terminal_response'])
        self.assertEqual((self.host / '.git/index').read_bytes(), self.index)

    def test_existing_acp_choice_model_and_owner_preferences_survive_review_and_update(self):
        self.run_command(['make', 'review'])
        config = json.loads(self.config_path.read_text())
        config.update(model='offline/owner', reviewers=3, timeout=47,
                      owner_preference={'keep': ['explicit', 'settings']})
        self.config_path.write_text(json.dumps(config))
        saved = self.config_path.read_bytes()
        self.environment.update(CLARITY_AGENT='codex', CLARITY_AGENT_COMMAND='["must-not-run"]')
        self.run_command(['make', 'review-staged'])
        self.assertEqual(self.result()[0]['status'], 'clear')
        self.assertEqual(self.config_path.read_bytes(), saved)
        selected = [call['selected_model'] for call in self.calls() if call['method'] == 'session/set_config_option']
        self.assertTrue(selected)
        self.assertEqual(set(selected), {'offline/owner'})
        self.run_command([str(self.host / '.clarity/update')])
        self.assertEqual(json.loads(self.config_path.read_text()), config)
        self.assertEqual((self.host / '.git/index').read_bytes(), self.index)

    def test_restored_current_keeps_acp_choice_and_retires_only_the_adopted_materials(self):
        self.run_command(['make', 'review'])
        before = json.loads(self.config_path.read_text())
        before.update(model='offline/owner', reviewers=3, timeout=47,
                      owner_preference={'keep': 'ACP settings'})
        self.config_path.write_text(json.dumps(before))
        shutil.rmtree(self.rule.parent)
        self.run_command([str(self.host / '.clarity/update')])
        reset = json.loads(self.config_path.read_text())
        self.assertTrue(reset['basis_reset'])
        self.assertNotIn('basis_snapshot', reset)
        for key in ('agent', 'agent_command', 'model', 'reviewers', 'timeout', 'owner_preference'):
            self.assertEqual(reset[key], before[key])
        self.assertEqual(self.directory_state(self.rule.parent), self.directory_state(self.source / 'initial'))
        self.environment.update(CLARITY_AGENT='codex', CLARITY_AGENT_COMMAND='["must-not-run"]')
        self.fill_rules()
        self.run_command(['make', 'review'])
        adopted = json.loads(self.config_path.read_text())
        self.assertNotIn('basis_reset', adopted)
        for key in ('agent', 'agent_command', 'model', 'reviewers', 'timeout', 'owner_preference'):
            self.assertEqual(adopted[key], before[key])
        self.assertEqual(adopted['basis_snapshot']['.clarity/current/rules.md']['text'], self.rule.read_text())
        self.assertEqual((self.host / '.git/index').read_bytes(), self.index)


    def test_existing_codex_binary_preference_uses_an_acp_adapter_without_rewriting_configuration(self):
        self.run_command(['make', 'review'])
        config = json.loads(self.config_path.read_text())
        config.pop('agent')
        config.pop('agent_command')
        legacy = self.binary / 'legacy-codex'
        legacy.write_text('#!/bin/sh\nexit 91\n')
        legacy.chmod(0o755)
        (self.binary / 'codex-acp').symlink_to(self.fake)
        config['codex_bin'] = str(legacy)
        self.config_path.write_text(json.dumps(config))
        saved = self.config_path.read_bytes()
        for call in self.fake.with_suffix('.calls').glob('*.json'):
            call.unlink()
        self.run_command(['make', 'review'])
        self.assertEqual(self.result()[0]['status'], 'clear')
        self.assertEqual(self.config_path.read_bytes(), saved)
        prompts = [call for call in self.calls() if call['method'] == 'session/prompt']
        self.assertEqual(len(prompts), 5)
        self.assertTrue(all(call['codex_path'] == str(legacy) for call in prompts))
        for call in prompts:
            if call['health']:
                self.assertEqual(call['mcp_servers'], [])
                continue
            self.assertEqual(len(call['mcp_servers']), 1)
            server = call['mcp_servers'][0]
            self.assertEqual(server['name'], 'clarity')
            self.assertEqual(server['args'][0], str(self.host / '.clarity/engine/runner/context_server.py'))
            self.assertEqual(Path(server['args'][server['args'].index('--root') + 1]).resolve(),
                             Path(call['context_root']).resolve())
        self.assertEqual((self.host / '.git/index').read_bytes(), self.index)


if __name__ == '__main__':
    unittest.main()
