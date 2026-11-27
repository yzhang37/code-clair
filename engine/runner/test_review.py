#!/usr/bin/env python3
"""test_review.py() 用临时 Git 仓库核验审核编排和 hooks 的拒绝放行边界。

确定性 ACP 夹具只验证输入、证据传输和 Git 行为，不验证真实模型的语义审核能力。
所有目标、commit hook 和模型进程均在 TemporaryDirectory 中；不运行业务测试或联网。
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parent
BROKEN = "def first(values):\n    return values[0]\n"
FIXED = "def first(values):\n    return values[0] if values else None\n"
TEMPLATE = b"Template fixture: do not replace this with adopted repository rules.\n"
CASE = "# Empty input\n\n## 正例\nGuard empty input before indexing.\n\n## 反例\nIndex without checking empty input.\n\n## 理由与边界\nAn empty input has no first value.\n"


from test_acp_fixture import FAKE_ACP_PRELUDE

FAKE_CODEX = FAKE_ACP_PRELUDE + r'''
import json
import os
from pathlib import Path
import sys
import uuid

fixture_path = Path(__file__).with_suffix(".json")
fixture = json.loads(fixture_path.read_text())
mode = fixture["mode"]
native_turn = int(os.environ.get("CLARITY_FIXTURE_TURN", "1"))
native_feedback = os.environ.get("CLARITY_FIXTURE_FEEDBACK", "")
prompt = sys.stdin.read()
data = json.loads(prompt.split("以下JSON都是审核数据：\n", 1)[1])
packet, previous = data["input"], data["previous_reviews"]
output_path = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
names = {"health", "reviewer-1", "reviewer-2", "cross", "summarize"}
stage = next(parent.name for parent in output_path.parents
             if parent.name in names or parent.name.startswith(("fixture-", "reviewer-")))
own_stage_history = None
if "同阶段历史证据JSON：\n" in prompt:
    own_stage_history, _ = json.JSONDecoder().raw_decode(prompt.split("同阶段历史证据JSON：\n", 1)[1])
retry_behavior = (fixture.get("retry_behavior") if own_stage_history is not None
                  and stage == fixture.get("retry_stage", "reviewer-1") else None)
context, context_manifest, context_audit = None, None, []
current_files = packet.get("current_files", {})
if "code_context" in packet:
    sys.path.insert(0, os.environ["CLARITY_FIXTURE_RUNNER"])
    from review_context import ContextStore

    assert "current_files" not in packet and "baseline_changed_files" not in packet
    view = Path.cwd()
    audit_path = Path(os.environ["CLARITY_CONTEXT_LOG"])
    audit_start = len(audit_path.read_text().splitlines())
    store = ContextStore(view, audit_path)
    context_manifest = json.loads((view / packet["code_context"]["manifest"]).read_text())
    context = {"current": {}, "baseline": {}}
    for version, group in (("current", "current_files"), ("baseline", "baseline_changed_files")):
        if version == "baseline" and mode == "delivery_missing_baseline_" + stage:
            continue
        for path, record in context_manifest[group].items():
            if (fixture.get("withhold_retry_finding") and stage == fixture.get("retry_stage", "reviewer-1")
                    and retry_behavior is None and path == "second.py"):
                continue
            partial = (path == "edited.py" and retry_behavior is None
                       and (mode == "delivery_partial_" + stage
                            or (mode in {"feedback_complete_" + stage, "feedback_drop_" + stage,
                                         "feedback_failure_" + stage} and native_turn == 1)))
            cursor = {"start_line": 1, "start_column": 1}
            pieces = []
            while True:
                arguments = {"path": path, "version": version, **cursor,
                             "line_count": 200, "char_limit": 1 if partial else 16000}
                payload = store.read_file(**arguments)
                transport_status = ("failed" if mode == "delivery_failed_ack_" + stage
                                    or (mode == "feedback_recover_ack_" + stage and native_turn == 1)
                                    else "completed")
                print(json.dumps({"type": "clarity.fixture.context_read", "arguments": arguments,
                                  "result": payload, "transport_status": transport_status}, ensure_ascii=False))
                pieces.append(payload["text"])
                if partial or payload["next"] is None:
                    break
                cursor = payload["next"]
            context[version][path] = "".join(pieces)
    context_audit = [json.loads(line) for line in audit_path.read_text().splitlines()[audit_start:]]
    with audit_path.open("a") as audit:
        if mode == "feedback_invalid_audit_" + stage:
            audit.write("invalid audit record\n")
    current_files = {path: "\n".join(f"{number}: {line}" for number, line in enumerate(text.splitlines(), 1))
                     for path, text in context["current"].items()}
calls = fixture_path.parent / "calls"
calls.mkdir(exist_ok=True)
(calls / (uuid.uuid4().hex + ".json")).write_text(json.dumps({
    "stage": stage, "mode": mode, "packet": packet, "previous_reviews": previous, "prompt": prompt,
    "context": context, "context_manifest": context_manifest, "current_files": current_files,
    "cwd_files": sorted(path.name for path in Path.cwd().iterdir()), "context_audit": context_audit,
    "own_stage_history": own_stage_history, "retry_behavior": retry_behavior,
    "native_turn": native_turn, "native_feedback": native_feedback,
}, ensure_ascii=False))
if retry_behavior == "adapter_failure":
    sys.exit(9)
if mode == "feedback_failure_" + stage and native_turn == 2:
    sys.exit(9)

if stage.startswith("fixture-"):
    if mode == "partial_fixture_failure" and stage == "fixture-0002":
        sys.exit(9)
    rule_path = next(iter(packet["adopted_rules"]))
    rule_quote = packet["adopted_rules"][rule_path].splitlines()[0].split(": ", 1)[1]
    lines = dict(line.split(": ", 1) for line in packet["fixture"].splitlines())
    checks = []
    for identifier, expectation in packet["expectations"].items():
        line = next(number for number in range(expectation["start_line"], expectation["end_line"] + 1)
                    if lines[str(number)].strip())
        checks.append({"id": identifier, "matches": True, "case_line": line,
                       "case_quote": lines[str(line)], "rule_path": rule_path, "rule_line": 1,
                       "rule_quote": rule_quote, "reason": "Deterministic semantic-check transport fixture."})
    mismatch = (mode == "fixture_mismatch"
                or (mode == "partial_fixture_failure" and stage == "fixture-0001")
                or (mode == "candidate_fixture_mismatch" and packet["view"] == "candidate-rules")
                or (mode == "candidate_case_mismatch" and packet["view"] == "candidate-case"))
    if mismatch or mode == "fixture_false_clear":
        checks[0]["matches"] = False
    result = {"snapshot_id": packet["snapshot_id"], "case_path": packet["case_path"],
              "status": "mismatched" if mismatch else "matched", "checks": checks,
              "summary": "Offline fixture; not a semantic quality claim.", "incomplete_reason": ""}
    if mode == "fixture_missing_expectation":
        result["checks"] = checks[:-1]
    elif mode == "fixture_wrong_case":
        result["case_path"] = ".clarity/current/cases/another.md"
    elif mode == "fixture_bad_rule":
        result["checks"][0]["rule_path"] = "not-selected.md"
    elif mode == "fixture_bad_case_quote":
        result["checks"][0]["case_quote"] = "not present in this fixture"
    output_path.write_text(json.dumps(result, ensure_ascii=False))
    for event in [{"type": "thread.started", "thread_id": "fixture"}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "fixture"}},
                  {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]:
        print(json.dumps(event))
    sys.exit(0)

health = stage == "health"
health_groups = ("adopted_rules", "fixtures", "candidate_rule_changes", "candidate_fixture_changes")
covered = (sorted({path for group in health_groups for path in packet[group]} | set(packet.get("basis_changes", {})))
           if health else list(context_manifest["current_files"] if context_manifest is not None else current_files))
findings = []
if not health:
    if previous:
        source = previous[-1:] if stage == "summarize" else previous
        findings = [dict(item) for report in source for item in report["findings"]]
    else:
        rule_path = next(iter(packet["adopted_rules"]))
        rule_quote = packet["adopted_rules"][rule_path].splitlines()[0].split(": ", 1)[1]
        for path, text in current_files.items():
            for numbered_line in text.splitlines():
                number, line = numbered_line.split(": ", 1)
                if line != "    return values[0]":
                    continue
                unchanged = path in packet["unchanged_paths"]
                findings.append({
                    "id": "empty-input:" + path,
                    "category": "boundary" if path == "second.py" else "correctness",
                    "title": "Empty-input fixture", "path": path, "line": int(number),
                    "question": "What protects the empty-input case?",
                    "reason": "The deterministic fixture identifies this exact source line.",
                    "code_quote": line, "rule_path": rule_path, "rule_line": 1,
                    "rule_quote": rule_quote,
                    "origin": "pre_existing" if unchanged else "introduced",
                    "baseline_line": int(number) if unchanged else 0,
                    "origin_reason": "The unchanged-path evidence determines this fixture origin.",
                    "counterevidence": "The fixture preserves the question; it does not assess model quality.",
                })

result = {
    "snapshot_id": packet["snapshot_id"],
    "status": "usable" if health else ("issues" if findings else "clear"),
    "covered_paths": covered, "findings": findings, "resolved": [],
    "unresolved_ids": [item["id"] for item in findings],
    "current_category": "correctness" if findings else "",
    "summary": "Deterministic protocol fixture; not semantic model validation.",
    "incomplete_reason": "",
}
if retry_behavior in {"drop", "resolved", "replace_with_new"}:
    result["findings"] = ([item for item in findings if item["path"] == "second.py"]
                          if retry_behavior == "replace_with_new" else [])
    result["resolved"] = ([{"id": item["id"], "reason": "Explicit same-stage offline counterevidence."}
                            for item in own_stage_history["findings"]] if retry_behavior == "resolved" else [])
    result["status"] = "issues" if result["findings"] else "clear"
    result["unresolved_ids"] = [item["id"] for item in result["findings"]]
    result["current_category"] = result["findings"][0]["category"] if result["findings"] else ""
if health and mode == "health_clear":
    result["status"] = "clear"
if health and mode == "health_missing_fixture":
    result["covered_paths"] = list(packet["adopted_rules"])
if health and mode == "health_missing_candidate":
    result["covered_paths"] = list(packet["adopted_rules"]) + list(packet["fixtures"])
if health and mode == "health_missing_change":
    omitted = {path for path, change in packet["basis_changes"].items()
               if change["before"] and change["before"]["empty"] and change["current"] is None}
    result["covered_paths"] = [path for path in covered if path not in omitted]
if health and (mode.startswith("health_quality_") or mode == "health_product_principle"):
    group = mode.removeprefix("health_quality_")
    if group not in health_groups:
        group = "adopted_rules"
    source = packet[group]
    path = fixture.get("quality_path", next(iter(source)))
    number, quote = next(line for line in reversed(source[path].splitlines()) if line.split(": ", 1)[1].strip()).split(": ", 1)
    rule_path, rule_text = next(iter(packet["principles"].items()))
    rule_line, rule_quote = rule_text.splitlines()[0].split(": ", 1)
    result.update(status="unhealthy", current_category="writing_quality")
    result["findings"] = [{
        "id": "quality:" + group, "category": "writing_quality", "title": "Writing-quality transport fixture",
        "path": path, "line": int(number), "code_quote": quote,
        "rule_path": rule_path, "rule_line": int(rule_line), "rule_quote": rule_quote,
        "question": "Does this document satisfy the supplied writing principle?",
        "reason": "The deterministic fixture checks quality evidence transport, not semantic quality.",
        "origin": "uncertain", "baseline_line": 0, "origin_reason": "Quality input group: " + group,
        "counterevidence": "The original document and supplied principle remain available for reconsideration.",
    }]
    result["unresolved_ids"] = [result["findings"][0]["id"]]
    if mode == "health_quality_product_rule":
        product_path, product_text = next(iter(packet["adopted_rules"].items()))
        result["findings"][0].update(rule_path=product_path, rule_line=1,
                                     rule_quote=product_text.splitlines()[0].split(": ", 1)[1])
    if mode == "health_product_principle":
        result["findings"][0]["category"] = "rule_conflict"
        result["current_category"] = "rule_conflict"
if stage == "reviewer-1":
    if mode == "missing_coverage":
        result["covered_paths"] = covered[:-1]
    elif mode == "invalid_reference":
        result["findings"][0]["path"] = "missing-from-snapshot.py"
    elif mode == "invalid_quote":
        result["findings"][0]["code_quote"] = "not present in the cited line"
    elif mode == "wrong_type":
        result["covered_paths"] = "not an array"
    elif mode == "model_failure":
        sys.exit(9)
if stage == "cross" and mode in {"cross_drop", "unresolved"}:
    result["findings"] = []
    result["status"] = "clear"
    result["current_category"] = ""
    result["unresolved_ids"] = []
    if mode == "unresolved":
        result["unresolved_ids"] = [item["id"] for report in previous for item in report["findings"]]
if stage == "cross" and mode in {"cross_resolved", "summary_failure_after_resolution"}:
    result["resolved"] = [{"id": findings[0]["id"], "reason": "Explicit offline counterevidence withdraws this fixture."}]
    result["findings"] = findings[1:]
    result["unresolved_ids"] = [item["id"] for item in result["findings"]]
if ((stage == "reviewer-2" and mode == "model_failure_reviewer2")
        or (stage == "cross" and mode == "cross_failure")
        or (stage == "summarize" and mode in {"summary_failure", "summary_failure_after_resolution"})):
    sys.exit(9)
if mode == "feedback_drop_" + stage and native_turn == 2:
    result.update(status="clear", findings=[], resolved=[], unresolved_ids=[], current_category="")
if mode == "case_as_rule_" + stage:
    case_path = next(path for path in current_files if path.startswith(".clarity/current/cases/"))
    case_quote = current_files[case_path].splitlines()[0].split(": ", 1)[1]
    result["findings"][0].update(rule_path=case_path, rule_line=1, rule_quote=case_quote)
if mode == "principle_as_rule_" + stage:
    result["findings"][0].update(rule_path="engine/principles.md", rule_line=1, rule_quote="# Principles")
if mode == "model_as_rule_" + stage:
    model_path, model_text = next(iter(packet["engine_model"].items()))
    result["findings"][0].update(rule_path=model_path, rule_line=1,
                                rule_quote=model_text.splitlines()[0].split(": ", 1)[1])

output_path.write_text("{invalid JSON" if ((stage == "reviewer-1" and mode == "invalid_json")
                       or (mode == "feedback_format_" + stage and native_turn == 1))
                       else json.dumps(result, ensure_ascii=False))
events = [{"type": "thread.started", "thread_id": "fixture"}, {"type": "turn.started"}]
if stage == "health" and mode == "tool_event":
    events.append({"type": "item.completed", "item": {"id": "forbidden", "type": "command_execution"}})
events.extend([
    {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "fixture"}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
])
for event in events:
    print(json.dumps(event))
'''


class IsolatedReviewTests(unittest.TestCase):
    """IsolatedReviewTests() 只把临时夹具仓库交给真实安装器和审核进程。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("GIT_") and key not in {"CODEX_API_KEY", "OPENAI_API_KEY"}}
        self.environment.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "CODEX_HOME": str(self.root / "empty-codex-home"),
            "OPENAI_API_KEY": "synthetic-offline-fixture-key",
        })
        self.fake = self.root / "fake-codex.py"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_CODEX)
        self.fake.chmod(0o755)
        self.environment.update(CLARITY_AGENT="codex", CLARITY_AGENT_COMMAND=json.dumps([str(self.fake)]))
        self.fixture_path = self.fake.with_suffix(".json")
        self.fixture_path.write_text(json.dumps({"mode": "normal"}))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Offline Fixture")
        self.git("config", "user.email", "offline-fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("rules.md", "Guard empty input before indexing values.\n")
        self.write("legacy.py", BROKEN)
        self.write("edited.py", "VERSION = 1\n")
        template = self.repo / ".clarity" / "initial" / "rules.md"
        template.parent.mkdir(parents=True)
        template.write_bytes(TEMPLATE)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "baseline")

    def execute(self, command, *, input_text=None, environment=None, check=True):
        result = subprocess.run(command, cwd=self.repo, env=environment or self.environment,
                                input=input_text, text=True, capture_output=True, timeout=30)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, *arguments, **kwargs):
        return self.execute(["git", "-C", str(self.repo), *arguments], **kwargs)

    def write(self, relative, content):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def install(self, *, check=True, basis="rules.md"):
        selections = [] if basis is None else ([basis] if isinstance(basis, str) else basis)
        result = self.execute([
            sys.executable, str(RUNNER / "install.py"), "--repo", str(self.repo),
            *[part for selection in selections for part in ("--basis", selection)],
            "--codex-bin", str(self.fake), "--model", "offline-fixture",
            "--timeout", "3",
        ], check=check)
        self.assertEqual((self.repo / ".clarity/initial/rules.md").read_bytes(), TEMPLATE)
        return result

    def commit_message_hook(self, action, *, check=True):
        """commit_message_hook() 在临时仓库运行真实 Shell 入口，不触碰宿主仓库的安装。"""
        return self.execute(["sh", str(RUNNER / "commit-msg"), action], check=check)

    def prepare_commit_message_installer(self):
        """prepare_commit_message_installer() 提供真实安装依赖和离线 CLI 占位，不启动模型进程。"""
        shutil.copytree(RUNNER.parent, self.repo / ".clarity/engine",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.write(".clarity/current/rules.md", "Guard empty input before indexing values.\n")
        executable = self.root / "bin/codex"
        executable.parent.mkdir()
        executable.write_text("#!/bin/sh\nexit 99\n")
        executable.chmod(0o755)
        self.environment["PATH"] = str(executable.parent) + os.pathsep + self.environment.get("PATH", os.defpath)

    @property
    def state(self):
        return self.repo / ".git" / "clarity"

    @property
    def installed_runner(self):
        return self.state / "engine" / "runner" / "review.py"

    def set_mode(self, mode):
        """set_mode() 同时更换模型配置，避免上一夹具的已验证缓存掩盖本轮故障。"""
        self.fixture_path.write_text(json.dumps({"mode": mode}))
        config_path = self.state / "config.json"
        config = json.loads(config_path.read_text())
        config["model"] = "offline-fixture-" + mode
        config_path.write_text(json.dumps(config))

    def review(self, *, environment=None):
        return self.execute([sys.executable, str(self.installed_runner), "review", "--repo", str(self.repo)],
                            environment=environment, check=False)

    def latest_result(self):
        latest = json.loads((self.state / "latest.json").read_text())
        return json.loads((Path(latest["run"]) / "result.json").read_text())

    def calls(self, mode=None):
        records = [json.loads(path.read_text()) for path in (self.root / "calls").glob("*.json")]
        return [record for record in records if mode is None or record["mode"] == mode]

    def test_complete_tracked_index_reports_an_unchanged_existing_problem(self):
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        self.write("edited.py", "VERSION = 999\n")
        self.write("legacy.py", FIXED)
        self.write("untracked.py", "UNTRACKED = True\n")
        result = self.review()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        calls = self.calls()
        self.assertFalse(any(call["stage"].startswith("fixture-") for call in calls))
        code_stages = {"reviewer-1", "reviewer-2", "cross", "summarize"}
        code_calls = [call for call in calls if call["stage"] in code_stages]
        self.assertEqual({call["stage"] for call in code_calls}, code_stages)
        for call in code_calls:
            self.assertEqual(call["packet"]["fixture_validation"],
                             {"snapshot_id": call["packet"]["snapshot_id"], "status": "no_coverage"})
            self.assertIn("no_coverage表示没有样例", call["prompt"])
        run = Path(json.loads((self.state / "latest.json").read_text())["run"])
        fixtures = json.loads((run / "fixtures.json").read_text())
        self.assertEqual(fixtures, {"snapshot_id": code_calls[0]["packet"]["snapshot_id"],
                                    "status": "no_coverage", "cases": [], "findings": []})
        initial = [call for call in calls if call["stage"].startswith("reviewer-")]
        self.assertEqual(len(initial), 2)
        for call in initial:
            packet = call["packet"]
            self.assertEqual(set(call["current_files"]),
                             {"rules.md", "legacy.py", "edited.py", ".clarity/initial/rules.md"})
            self.assertIn("2:     return values[0]", call["current_files"]["legacy.py"])
            self.assertEqual(call["current_files"]["edited.py"], "1: VERSION = 2")
            self.assertIn("legacy.py", packet["unchanged_paths"])
        report = self.latest_result()
        self.assertEqual(report["status"], "issues")
        self.assertTrue(report["findings"])
        self.assertTrue(all(item["origin"] == "pre_existing" for item in report["findings"]))

    def test_custom_agent_keeps_complete_inline_snapshot_and_closed_cwd(self):
        """test_custom_agent_keeps_complete_inline_snapshot_and_closed_cwd() 核对 custom 仍接收完整固定正文，不取得原生工具视图。"""
        self.environment["CLARITY_AGENT"] = "custom"
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        self.write("edited.py", "VERSION = 999\n")
        self.write("legacy.py", FIXED)
        self.write("untracked.py", "UNTRACKED_MUST_NOT_ENTER_CUSTOM = True\n")
        result = self.review()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.latest_result()["status"], "issues")
        calls = [call for call in self.calls() if call["stage"] != "health"]
        self.assertEqual({call["stage"] for call in calls}, {"reviewer-1", "reviewer-2", "cross", "summarize"})
        expected = {"rules.md": "1: Guard empty input before indexing values.",
                    "legacy.py": "1: def first(values):\n2:     return values[0]",
                    "edited.py": "1: VERSION = 2", ".clarity/initial/rules.md": "1: " + TEMPLATE.decode().strip()}
        for call in calls:
            packet = call["packet"]
            self.assertEqual(packet["current_files"], expected)
            self.assertEqual(packet["baseline_changed_files"], {"edited.py": "1: VERSION = 1"})
            self.assertEqual(call["current_files"], expected)
            self.assertNotIn("code_context", packet)
            self.assertIsNone(call["context"])
            self.assertIsNone(call["context_manifest"])
            self.assertEqual(call["cwd_files"], [])
            self.assertIn("不用也不允许调用任何工具", call["prompt"])
            self.assertNotIn("UNTRACKED_MUST_NOT_ENTER_CUSTOM", call["prompt"])
        self.assertTrue(all(item["origin"] == "pre_existing" for item in self.latest_result()["findings"]))
        run = Path(json.loads((self.state / "latest.json").read_text())["run"])
        self.assertFalse(list(run.glob("*/context-delivery.json")))
        self.assertFalse(list(run.glob("*/attempt-*/context-access.jsonl")))
        for events_path in run.glob("*/attempt-*/events.jsonl"):
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            self.assertFalse(any(event.get("update", {}).get("sessionUpdate") in
                                 {"tool_call", "tool_call_update"} for event in events))

    def test_custom_oversized_inline_snapshot_stops_before_model_without_truncation(self):
        """test_custom_oversized_inline_snapshot_stops_before_model_without_truncation() 保留 custom 全文容量边界，不能静默改成视图或截断后放行。"""
        self.environment.update(CLARITY_AGENT="custom", CLARITY_LANG="en")
        self.install()
        self.write("oversized.txt", "x" * 1_500_001)
        self.git("add", "oversized.txt")
        result = self.review()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = self.latest_result()
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("Complete snapshot exceeds", report["incomplete_reason"])
        self.assertIn("nothing was truncated or automatically passed", report["incomplete_reason"])
        self.assertEqual(self.calls(), [])

    def test_code_context_contains_exact_selected_versions_without_inline_source(self):
        """test_code_context_contains_exact_selected_versions_without_inline_source() 逐版核对冻结原文，防止工作树或提示副本替代选定索引。"""
        self.install()
        added = "nested α/acquired.py"
        selected = "CONTEXT_ONLY_MARKER = 'selected bytes'\r\nVALUE = 2\n"
        self.write(added, selected)
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", added, "edited.py")
        self.git("rm", "--cached", "legacy.py")
        self.write(added, "WORKTREE_ONLY_MARKER = True\n")
        self.write("legacy.py", "DELETED_INDEX_WORKTREE_MARKER = True\n")
        result = self.review()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.latest_result()["status"], "clear")
        expected_current = {"rules.md": "Guard empty input before indexing values.\n",
                            "edited.py": "VERSION = 2\n", added: selected,
                            ".clarity/initial/rules.md": TEMPLATE.decode()}
        expected_baseline = {"edited.py": "VERSION = 1\n", "legacy.py": BROKEN}
        calls = self.calls()
        code_calls = [call for call in calls if call["stage"] != "health"]
        self.assertEqual({call["stage"] for call in code_calls}, {"reviewer-1", "reviewer-2", "cross", "summarize"})
        for call in code_calls:
            packet, context = call["packet"], call["context"]
            self.assertNotIn("current_files", packet)
            self.assertNotIn("baseline_changed_files", packet)
            self.assertEqual(context, {"current": expected_current, "baseline": expected_baseline})
            self.assertEqual(packet["code_context"]["current_file_count"], len(expected_current))
            self.assertEqual(packet["code_context"]["baseline_changed_file_count"], len(expected_baseline))
            self.assertNotIn("CONTEXT_ONLY_MARKER", call["prompt"])
            self.assertNotIn("WORKTREE_ONLY_MARKER", call["prompt"])
            self.assertNotIn("DELETED_INDEX_WORKTREE_MARKER", call["prompt"])
            for group, files in (("current_files", expected_current), ("baseline_changed_files", expected_baseline)):
                manifest = call["context_manifest"][group]
                self.assertEqual(set(manifest), set(files))
                for path, text in files.items():
                    self.assertEqual(manifest[path]["bytes"], len(text.encode()))
                    self.assertEqual(manifest[path]["lines"], len(text.splitlines()))
                    self.assertEqual(manifest[path]["oid"], self.git("hash-object", "--stdin", input_text=text).stdout.strip())
                    self.assertEqual(manifest[path]["mode"], "100644")
        health = next(call for call in calls if call["stage"] == "health")
        self.assertNotIn("code_context", health["packet"])
        self.assertIsNone(health["context"])

    def test_partial_source_delivery_cannot_pass_and_retains_valid_findings_at_each_code_stage(self):
        """test_partial_source_delivery_cannot_pass_and_retains_valid_findings_at_each_code_stage() 区分自报路径覆盖和原文交付，缺口不能抹去已核验发现。"""
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        for stage in ("reviewer-1", "cross", "summarize"):
            with self.subTest(stage=stage):
                mode = "delivery_partial_" + stage
                self.set_mode(mode)
                completed = self.review()
                self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
                report = self.latest_result()
                self.assertEqual(report["status"], "incomplete")
                self.assertIn("Frozen source delivery is incomplete in " + stage, report["incomplete_reason"])
                self.assertEqual({item["id"] for item in report["findings"]},
                                 {"reviewer-1:empty-input:legacy.py", "reviewer-2:empty-input:legacy.py"})
                self.assertTrue(all(item["code_quote"] == "    return values[0]" and item["rule_path"] == "rules.md"
                                    for item in report["findings"]))
                run = Path(json.loads((self.state / "latest.json").read_text())["run"])
                validated = json.loads((run / stage / "validated.json").read_text())
                call = next(call for call in self.calls(mode) if call["stage"] == stage)
                self.assertEqual(set(validated["covered_paths"]), set(call["context_manifest"]["current_files"]))
                self.assertEqual(call["context"]["current"]["edited.py"], "V")
                delivery = json.loads((run / stage / "context-delivery.json").read_text())["delivery"]
                self.assertFalse(delivery["complete"])
                self.assertTrue(delivery["audit_valid"])
                self.assertEqual({item["path"] for item in delivery["current"]["missing"]}, {"edited.py"})
                self.assertEqual({item["path"] for item in delivery["baseline"]["missing"]}, {"edited.py"})
                if stage == "reviewer-1":
                    self.assertFalse(any(call["stage"] in {"cross", "summarize"} for call in self.calls(mode)))
                if stage == "cross":
                    self.assertFalse(any(call["stage"] == "summarize" for call in self.calls(mode)))

    def test_complete_current_delivery_still_requires_changed_baseline_text(self):
        """test_complete_current_delivery_still_requires_changed_baseline_text() 完整当前正文不能替代来源判断所需的已改变基线。"""
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        self.set_mode("delivery_missing_baseline_reviewer-1")
        completed = self.review()
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        report = self.latest_result()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(len(report["findings"]), 2)
        run = Path(json.loads((self.state / "latest.json").read_text())["run"])
        delivery = json.loads((run / "reviewer-1/context-delivery.json").read_text())["delivery"]
        self.assertTrue(delivery["audit_valid"])
        self.assertEqual(delivery["current"]["complete"], delivery["current"]["total"])
        self.assertEqual(delivery["baseline"]["total"], 1)
        self.assertEqual(delivery["baseline"]["complete"], 0)
        self.assertEqual({item["path"] for item in delivery["baseline"]["missing"]}, {"edited.py"})
        self.assertFalse(delivery["complete"])
        self.assertFalse(any(call["stage"] in {"cross", "summarize"} for call in self.calls()))

    def test_same_session_feedback_completes_missing_source_and_reuses_complete_cache(self):
        """test_same_session_feedback_completes_missing_source_and_reuses_complete_cache() 保留首轮有效发现并补齐原文，后续缓存仍绑定同一初始任务。"""
        from review import digest

        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        self.set_mode("feedback_complete_reviewer-1")
        completed = self.review()
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        calls = sorted((call for call in self.calls() if call["stage"] == "reviewer-1"), key=lambda call: call["native_turn"])
        self.assertEqual([call["native_turn"] for call in calls], [1, 2])
        self.assertEqual(calls[0]["prompt"], calls[1]["prompt"])
        self.assertEqual(calls[0]["context"]["current"]["edited.py"], "V")
        self.assertEqual(calls[1]["context"]["current"]["edited.py"], "VERSION = 2\n")
        self.assertIn("original prior reviews and the first reply", calls[1]["native_feedback"])
        run = Path(json.loads((self.state / "latest.json").read_text())["run"])
        records = sorted((run / "reviewer-1").glob("attempt-*/validated-stage.json"))
        self.assertEqual(len(records), 2)
        self.assertTrue(all(json.loads(path.read_text())["accounted"] for path in records))
        final = records[-1].parent
        provenance = json.loads((final / "native-turn.json").read_text())
        self.assertFalse(provenance["independent_session"])
        self.assertEqual(provenance["feedback_sha256"], digest((final / "feedback.txt").read_text()))
        self.assertEqual(provenance["initial_prompt_sha256"], (run / "reviewer-1/prompt.sha256").read_text())
        self.assertTrue((run / "reviewer-1" / provenance["protocol_events"]).is_file())
        self.assertTrue((run / "reviewer-1" / provenance["context_audit"]).is_file())
        self.assertTrue(json.loads((run / "reviewer-1/context-delivery.json").read_text())["delivery"]["complete"])
        for stage in ("reviewer-2", "cross", "summarize"):
            self.assertEqual(len(list((run / stage).glob("attempt-*/validated-stage.json"))), 1)
            self.assertFalse(list((run / stage).glob("attempt-*/native-turn.json")))
        before = len(self.calls())
        self.assertEqual(self.review().returncode, 1)
        self.assertEqual(len(self.calls()), before)

    def test_server_read_with_only_failed_acp_results_cannot_pass_or_cache(self):
        """test_server_read_with_only_failed_acp_results_cannot_pass_or_cache() 服务成功读到全文但 Agent 只收到失败结果时，保持未完成且不可缓存。"""
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        self.set_mode("delivery_failed_ack_reviewer-1")
        completed = self.review()
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        run = Path(json.loads((self.state / "latest.json").read_text())["run"])
        receipt = json.loads((run / "reviewer-1/context-delivery.json").read_text())["delivery"]
        self.assertTrue(receipt["audit_valid"])
        self.assertTrue(receipt["transport_confirmed"])
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["current"]["complete"], 0)
        attempts = list((run / "reviewer-1").glob("attempt-*/context-access.jsonl"))
        self.assertEqual(len(attempts), 1)
        audit = [json.loads(line) for line in attempts[0].read_text().splitlines()]
        self.assertTrue(any(row.get("returned_chars", 0) > 0 for row in audit))
        events = [json.loads(line) for line in attempts[0].with_name("events.jsonl").read_text().splitlines()]
        terminal = [event["update"]["status"] for event in events
                    if event.get("update", {}).get("sessionUpdate") == "tool_call_update"]
        self.assertTrue(terminal)
        self.assertEqual(set(terminal), {"failed"})
        before = sum(call["stage"] == "reviewer-1" for call in self.calls())
        self.assertEqual(self.review().returncode, 2)
        self.assertEqual(sum(call["stage"] == "reviewer-1" for call in self.calls()), before + 2)

    def test_failed_source_transport_requires_later_completed_exact_pages(self):
        """test_failed_source_transport_requires_later_completed_exact_pages() 同会话补读必须实际完成并交付全部真实分页，失败首轮的服务记录不补认正文。"""
        self.install()
        text = "# " + "续" * 17000 + "\nVERSION = 2\n"
        self.write("edited.py", text)
        self.git("add", "edited.py")
        self.set_mode("feedback_recover_ack_reviewer-1")
        completed = self.review()
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        calls = sorted((call for call in self.calls() if call["stage"] == "reviewer-1"), key=lambda call: call["native_turn"])
        self.assertEqual([call["native_turn"] for call in calls], [1, 2])
        self.assertTrue(all(call["context"]["current"]["edited.py"] == text for call in calls))
        self.assertTrue(all(sum(row["path"] == "edited.py" and row["version"] == "current"
                                for row in call["context_audit"]) >= 2 for call in calls))
        run = Path(json.loads((self.state / "latest.json").read_text())["run"])
        delivery = json.loads((run / "reviewer-1/context-delivery.json").read_text())["delivery"]
        self.assertTrue(delivery["complete"])
        self.assertTrue(delivery["transport_confirmed"])
        protocol = next((run / "reviewer-1").glob("attempt-*/events.jsonl"))
        events = [json.loads(line) for line in protocol.read_text().splitlines()]
        terminal = [event["update"]["status"] for event in events
                    if event.get("update", {}).get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", terminal)
        self.assertIn("completed", terminal)
        self.assertLess(terminal.index("failed"), terminal.index("completed"))

    def test_same_session_feedback_can_repair_json_without_retaining_invalid_first_output(self):
        """test_same_session_feedback_can_repair_json_without_retaining_invalid_first_output() 无效首轮 JSON 只触发结构反馈，不成为已核验发现。"""
        self.install()
        self.set_mode("feedback_format_reviewer-1")
        completed = self.review()
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        calls = sorted((call for call in self.calls() if call["stage"] == "reviewer-1"), key=lambda call: call["native_turn"])
        self.assertEqual([call["native_turn"] for call in calls], [1, 2])
        self.assertTrue(calls[1]["native_feedback"])
        run = Path(json.loads((self.state / "latest.json").read_text())["run"])
        records = list((run / "reviewer-1").glob("attempt-*/validated-stage.json"))
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].parent.name.endswith("-turn-2"))

    def test_same_session_feedback_cannot_drop_first_verified_ids(self):
        """test_same_session_feedback_cannot_drop_first_verified_ids() 补齐正文不能代替旧发现的保留或反证，原 account_for 继续拒绝漏项。"""
        self.environment["CLARITY_LANG"] = "en"
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        self.set_mode("feedback_drop_reviewer-1")
        completed = self.review()
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        report = self.latest_result()
        self.assertIn("omitted the disposition", report["incomplete_reason"])
        self.assertEqual({item["id"] for item in report["findings"]},
                         {"reviewer-1:empty-input:legacy.py", "reviewer-2:empty-input:legacy.py"})
        run = Path(json.loads((self.state / "latest.json").read_text())["run"])
        records = [json.loads(path.read_text()) for path in sorted((run / "reviewer-1").glob("attempt-*/validated-stage.json"))]
        self.assertEqual([record["accounted"] for record in records], [True, False])
        self.assertTrue(records[0]["result"]["findings"])
        self.assertEqual(records[1]["result"]["findings"], [])

    def test_same_session_feedback_failure_or_invalid_audit_retains_first_valid_findings(self):
        """test_same_session_feedback_failure_or_invalid_audit_retains_first_valid_findings() 续接失败及反馈前损坏审计都保留已核验首轮证据。"""
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        for mode in ("feedback_failure_reviewer-1", "feedback_invalid_audit_reviewer-1"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                completed = self.review()
                self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
                self.assertEqual({item["id"] for item in self.latest_result()["findings"]},
                                 {"reviewer-1:empty-input:legacy.py", "reviewer-2:empty-input:legacy.py"})
                run = Path(json.loads((self.state / "latest.json").read_text())["run"])
                records = list((run / "reviewer-1").glob("attempt-*/validated-stage.json"))
                self.assertEqual(len(records), 1)
                record = json.loads(records[0].read_text())
                self.assertTrue(record["accounted"])
                self.assertTrue(record["result"]["findings"])

    def test_same_snapshot_context_retry_failure_retains_own_stage_findings(self):
        """test_same_snapshot_context_retry_failure_retains_own_stage_findings() 重取调用失败仍保留同阶段原已核验发现，不把旧 partial 当成新成功。"""
        from review import digest

        self.environment["CLARITY_LANG"] = "en"
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        for stage in ("reviewer-1", "cross", "summarize"):
            with self.subTest(stage=stage):
                self.set_mode("delivery_partial_" + stage)
                self.assertEqual(self.review().returncode, 2)
                before = self.latest_result()
                run = Path(json.loads((self.state / "latest.json").read_text())["run"])
                prior_record = next((run / stage).glob("attempt-*/validated-stage.json"))
                original = prior_record.read_bytes()
                settings = json.loads(self.fixture_path.read_text())
                settings.update(retry_stage=stage, retry_behavior="adapter_failure")
                self.fixture_path.write_text(json.dumps(settings))
                completed = self.review()
                self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
                after = self.latest_result()
                self.assertEqual(after["status"], "incomplete")
                self.assertEqual(after["snapshot_id"], before["snapshot_id"])
                self.assertEqual({item["id"] for item in after["findings"]}, {item["id"] for item in before["findings"]})
                self.assertEqual(prior_record.read_bytes(), original)
                retry = next(call for call in self.calls(settings["mode"]) if call["retry_behavior"] == "adapter_failure")
                self.assertEqual(retry["own_stage_history"]["stage"], stage)
                if stage == "reviewer-1":
                    self.assertEqual(retry["previous_reviews"], [])
                    self.assertEqual({item["id"] for item in retry["own_stage_history"]["findings"]}, {"empty-input:legacy.py"})
                attempt = sorted((run / stage).glob("attempt-*"))[-1]
                request = json.loads((attempt / "request.json").read_text())
                self.assertEqual(request["base_prompt_sha256"], (run / stage / "base-prompt.sha256").read_text())
                self.assertEqual(request["prompt_sha256"], digest((attempt / "review-prompt.txt").read_text()))
                self.assertNotEqual(request["base_prompt_sha256"], request["prompt_sha256"])
                self.assertFalse((attempt / "validated-stage.json").exists())

    def test_same_snapshot_context_retry_clear_must_account_for_old_findings(self):
        """test_same_snapshot_context_retry_clear_must_account_for_old_findings() 新 clear 没有保留或解除旧 ID 时拒绝，并持久保留旧引用。"""
        self.environment["CLARITY_LANG"] = "en"
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        for stage in ("reviewer-1", "cross", "summarize"):
            with self.subTest(stage=stage):
                self.set_mode("delivery_partial_" + stage)
                self.assertEqual(self.review().returncode, 2)
                before = self.latest_result()
                settings = json.loads(self.fixture_path.read_text())
                settings.update(retry_stage=stage, retry_behavior="drop")
                self.fixture_path.write_text(json.dumps(settings))
                completed = self.review()
                self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
                after = self.latest_result()
                self.assertEqual(after["snapshot_id"], before["snapshot_id"])
                self.assertEqual({item["id"] for item in after["findings"]}, {item["id"] for item in before["findings"]})
                self.assertIn("omitted the disposition", after["incomplete_reason"])
                run = Path(json.loads((self.state / "latest.json").read_text())["run"])
                record = json.loads(sorted((run / stage).glob("attempt-*/validated-stage.json"))[-1].read_text())
                self.assertFalse(record["accounted"])
                self.assertEqual(record["result"]["status"], "clear")
                self.assertEqual(record["result"]["resolved"], [])

    def test_same_snapshot_context_retry_resolution_and_full_delivery_can_continue(self):
        """test_same_snapshot_context_retry_resolution_and_full_delivery_can_continue() 明确反证解除旧 ID 且本轮全文已交付，才按原流程继续审核。"""
        self.environment["CLARITY_LANG"] = "en"
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        for stage in ("reviewer-1", "cross", "summarize"):
            with self.subTest(stage=stage):
                self.set_mode("delivery_partial_" + stage)
                self.assertEqual(self.review().returncode, 2)
                before = self.latest_result()
                settings = json.loads(self.fixture_path.read_text())
                settings.update(retry_stage=stage, retry_behavior="resolved")
                self.fixture_path.write_text(json.dumps(settings))
                completed = self.review()
                self.assertEqual(completed.returncode, 1 if stage == "reviewer-1" else 0, completed.stdout + completed.stderr)
                after = self.latest_result()
                self.assertEqual(after["snapshot_id"], before["snapshot_id"])
                self.assertEqual({item["id"] for item in after["findings"]},
                                 {"reviewer-2:empty-input:legacy.py"} if stage == "reviewer-1" else set())
                run = Path(json.loads((self.state / "latest.json").read_text())["run"])
                receipt = json.loads((run / stage / "context-delivery.json").read_text())
                self.assertTrue(receipt["delivery"]["complete"])
                self.assertEqual(receipt["base_prompt_sha256"], (run / stage / "base-prompt.sha256").read_text())
                self.assertEqual(receipt["prompt_sha256"], (run / stage / "prompt.sha256").read_text())
                self.assertNotEqual(receipt["base_prompt_sha256"], receipt["prompt_sha256"])
                record = json.loads(sorted((run / stage).glob("attempt-*/validated-stage.json"))[-1].read_text())
                self.assertTrue(record["accounted"])
                self.assertTrue(record["result"]["resolved"])
                calls = len(self.calls())
                self.assertEqual(self.review().returncode, completed.returncode)
                self.assertEqual(len(self.calls()), calls)

    def test_same_snapshot_context_retries_preserve_new_valid_findings_until_explicit_resolution(self):
        """test_same_snapshot_context_retries_preserve_new_valid_findings_until_explicit_resolution() 连续重取保留漏交代旧 ID 时的新有效发现，后续调用失败也不能丢失。"""
        self.environment["CLARITY_LANG"] = "en"
        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.write("second.py", BROKEN)
        self.git("add", "edited.py", "second.py")
        self.set_mode("delivery_partial_reviewer-1")
        settings = json.loads(self.fixture_path.read_text())
        settings["withhold_retry_finding"] = True
        self.fixture_path.write_text(json.dumps(settings))
        self.assertEqual(self.review().returncode, 2)
        initial = self.latest_result()
        self.assertEqual(len(initial["findings"]), 3)
        expected = {"reviewer-1:empty-input:legacy.py", "reviewer-1:empty-input:second.py",
                    "reviewer-2:empty-input:legacy.py", "reviewer-2:empty-input:second.py"}
        for behavior in ("replace_with_new", "adapter_failure"):
            settings["retry_behavior"] = behavior
            self.fixture_path.write_text(json.dumps(settings))
            completed = self.review()
            self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
            report = self.latest_result()
            self.assertEqual(report["snapshot_id"], initial["snapshot_id"])
            self.assertEqual({item["id"] for item in report["findings"]}, expected)
            self.assertTrue(all(item["code_quote"] == "    return values[0]" for item in report["findings"]))
        last = next(call for call in reversed(self.calls()) if call["retry_behavior"] == "adapter_failure")
        self.assertEqual({item["id"] for item in last["own_stage_history"]["findings"]},
                         {"empty-input:legacy.py", "empty-input:second.py"})
        settings["retry_behavior"] = "resolved"
        self.fixture_path.write_text(json.dumps(settings))
        completed = self.review()
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        report = self.latest_result()
        self.assertEqual(report["snapshot_id"], initial["snapshot_id"])
        self.assertEqual({item["id"] for item in report["findings"]},
                         {"reviewer-2:empty-input:legacy.py", "reviewer-2:empty-input:second.py"})

    def test_context_cache_requires_matching_complete_delivery_receipt(self):
        """test_context_cache_requires_matching_complete_delivery_receipt() 损坏或自相矛盾的回执不能绕过交付 gate，只有完整同版证据可复用。"""
        from review import Incomplete, require_context_delivery

        self.install()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        self.assertEqual(self.review().returncode, 1)
        run = Path(json.loads((self.state / "latest.json").read_text())["run"])
        calls = self.calls()
        first = next(call for call in calls if call["stage"] == "reviewer-1")
        context = {version: {path: {"text": text,
                                    "oid": first["context_manifest"][group][path]["oid"],
                                    "mode": first["context_manifest"][group][path]["mode"]}
                             for path, text in first["context"][version].items()}
                   for version, group in (("current", "current_files"), ("baseline", "baseline_changed_files"))}
        require_context_delivery(run, "reviewer-1", context)
        self.assertEqual(self.review().returncode, 1)
        self.assertEqual(len(self.calls()), len(calls))
        for mismatch in ("missing", "prompt", "output", "partial", "minimal", "invalid_audit",
                         "missing_transport", "false_transport",
                         "reported_gap", "audit_errors", "zero_counts", "boolean_counts"):
            with self.subTest(mismatch=mismatch):
                receipt_path = run / "reviewer-1/context-delivery.json"
                if mismatch == "missing":
                    receipt_path.unlink()
                else:
                    receipt = json.loads(receipt_path.read_text())
                    delivery = receipt["delivery"]
                    if mismatch in {"prompt", "output"}:
                        receipt[mismatch + "_sha256"] = "0" * 64
                    elif mismatch == "partial":
                        delivery["complete"] = False
                    elif mismatch == "minimal":
                        receipt["delivery"] = {"complete": True}
                    elif mismatch == "invalid_audit":
                        delivery["audit_valid"] = False
                    elif mismatch == "missing_transport":
                        delivery.pop("transport_confirmed")
                    elif mismatch == "false_transport":
                        delivery["transport_confirmed"] = False
                    elif mismatch == "reported_gap":
                        total = len(context["current"]["edited.py"]["text"])
                        delivery["current"]["missing"] = [{"path": "edited.py", "total_chars": total,
                            "delivered_chars": 1, "missing_ranges": [{"start": 1, "end": total}]}]
                    elif mismatch == "audit_errors":
                        delivery["errors"] = [{"line": 1, "reason": "invalid_audit_record"}]
                    elif mismatch == "zero_counts":
                        delivery["current"] = {"total": 0, "complete": 0, "missing": []}
                        delivery["baseline"] = {"total": 0, "complete": 0, "missing": []}
                    elif mismatch == "boolean_counts":
                        self.assertEqual(len(context["baseline"]), 1)
                        delivery["baseline"] = {"total": True, "complete": True, "missing": []}
                    receipt_path.write_text(json.dumps(receipt))
                with self.assertRaises((Incomplete, FileNotFoundError)):
                    require_context_delivery(run, "reviewer-1", context)
                before = {stage: sum(call["stage"] == stage for call in self.calls())
                          for stage in ("health", "reviewer-1", "reviewer-2", "cross", "summarize")}
                completed = self.review()
                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                after = {stage: sum(call["stage"] == stage for call in self.calls()) for stage in before}
                self.assertEqual(after, {**before, "reviewer-1": before["reviewer-1"] + 1})
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt["prompt_sha256"], (run / "reviewer-1/prompt.sha256").read_text())
                self.assertTrue(receipt["delivery"]["complete"])
                self.assertTrue(receipt["delivery"]["audit_valid"])
                require_context_delivery(run, "reviewer-1", context)

    def test_independent_initial_inputs_then_cross_review_preserves_each_finding(self):
        self.write("second.py", BROKEN)
        self.git("add", "second.py")
        self.install()
        self.assertEqual(self.review().returncode, 1)
        calls = self.calls()
        health = next(call for call in calls if call["stage"] == "health")["packet"]
        basis = set(json.loads((self.state / "config.json").read_text())["basis"])
        for call in calls:
            if call["stage"] == "health":
                continue
            packet = call["packet"]
            self.assertIn("basis_health", packet, call["stage"])
            self.assertEqual(packet["basis_health"]["status"], "usable", call["stage"])
            self.assertEqual(packet["basis_health"]["snapshot_id"], packet["snapshot_id"], call["stage"])
            self.assertEqual(packet["basis_health"]["snapshot_id"], health["snapshot_id"], call["stage"])
            self.assertEqual(set(packet["basis_health"]["covered_paths"]), basis, call["stage"])
        initial = [call for call in calls if call["stage"].startswith("reviewer-")]
        self.assertEqual(len(initial), 2)
        self.assertTrue(all(call["previous_reviews"] == [] for call in initial))
        cross = next(call for call in calls if call["stage"] == "cross")
        self.assertEqual(len(cross["previous_reviews"]), 2)
        for output in cross["previous_reviews"]:
            self.assertEqual(set(output["unresolved_ids"]), {item["id"] for item in output["findings"]})
        initial_ids = {finding["id"] for report in cross["previous_reviews"] for finding in report["findings"]}
        self.assertEqual(len(initial_ids), 4)
        report = self.latest_result()
        self.assertEqual({finding["id"] for finding in report["findings"]}, initial_ids)
        self.assertEqual(set(report["unresolved_ids"]), initial_ids)
        self.assertEqual({finding["category"] for finding in report["findings"]}, {"correctness", "boundary"})
        self.assertEqual(len(next(call for call in calls if call["stage"] == "summarize")["previous_reviews"]), 3)

    def test_incomplete_protocols_and_unresolved_findings_never_pass(self):
        self.install()
        modes = ("health_clear", "invalid_json", "invalid_reference", "invalid_quote", "missing_coverage",
                 "wrong_type", "model_failure", "tool_event", "cross_drop", "unresolved")
        for mode in modes:
            with self.subTest(mode=mode):
                self.set_mode(mode)
                result = self.review()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.latest_result()["status"], "incomplete")
                if mode == "health_clear":
                    self.assertEqual({call["stage"] for call in self.calls(mode)}, {"health"})
                if mode in {"cross_drop", "unresolved"}:
                    self.assertNotIn("summarize", {call["stage"] for call in self.calls(mode)})

    def test_partial_independent_failure_keeps_only_validated_findings(self):
        """test_partial_independent_failure_keeps_only_validated_findings() 保留成功首审，不采纳失败或引用无效的输出。"""
        self.install()
        for mode, surviving_reviewer in [("model_failure", "reviewer-2"),
                                         ("invalid_reference", "reviewer-2"),
                                         ("invalid_json", "reviewer-2"),
                                         ("model_failure_reviewer2", "reviewer-1")]:
            with self.subTest(mode=mode):
                self.set_mode(mode)
                self.assertEqual(self.review().returncode, 2)
                report = self.latest_result()
                self.assertEqual(report["status"], "incomplete")
                self.assertEqual(len(report["findings"]), 1)
                self.assertTrue(report["findings"][0]["id"].startswith(surviving_reviewer + ":"))
                self.assertEqual(report["findings"][0]["path"], "legacy.py")
                self.assertIn("独立代码首审", report["incomplete_reason"])
                self.assertIn("待核实发现", report["summary"])
                self.assertFalse(any(call["stage"] == "cross" for call in self.calls(mode)))

    def test_cross_or_summary_failure_keeps_last_valid_findings(self):
        """test_cross_or_summary_failure_keeps_last_valid_findings() 后续故障保留已有发现，同时维持未完成状态。"""
        self.install()
        for mode, scope in [("cross_failure", "交叉反证"), ("cross_drop", "交叉反证"),
                            ("unresolved", "交叉反证"), ("summary_failure", "汇总")]:
            with self.subTest(mode=mode):
                self.set_mode(mode)
                self.assertEqual(self.review().returncode, 2)
                report = self.latest_result()
                self.assertEqual(report["status"], "incomplete")
                self.assertEqual({finding["id"] for finding in report["findings"]},
                                 {"reviewer-1:empty-input:legacy.py", "reviewer-2:empty-input:legacy.py"})
                self.assertIn(scope, report["incomplete_reason"])

    def test_valid_cross_resolution_is_not_restored_after_summary_failure(self):
        """test_valid_cross_resolution_is_not_restored_after_summary_failure() 有效反证撤回的问题不因后续故障复活。"""
        self.install()
        for mode, expected_status, expected_code in [("cross_resolved", "issues", 1),
                                                     ("summary_failure_after_resolution", "incomplete", 2)]:
            with self.subTest(mode=mode):
                self.set_mode(mode)
                self.assertEqual(self.review().returncode, expected_code)
                report = self.latest_result()
                self.assertEqual(report["status"], expected_status)
                self.assertEqual([finding["id"] for finding in report["findings"]],
                                 ["reviewer-2:empty-input:legacy.py"])

    def test_partial_fixture_failure_keeps_completed_mismatch(self):
        """test_partial_fixture_failure_keeps_completed_mismatch() 逐案例核对中断仍保留此前已验证的不匹配。"""
        self.adopt_current_fixtures(2)
        self.set_mode("partial_fixture_failure")
        self.assertEqual(self.review().returncode, 2)
        report = self.latest_result()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["category"], "fixture_rule_mismatch")
        self.assertEqual(report["findings"][0]["path"], ".clarity/current/cases/0001.md")
        self.assertIn("fixtures 兼容性核对", report["incomplete_reason"])
        self.assertFalse(any(call["stage"].startswith("reviewer-") for call in self.calls("partial_fixture_failure")))

    def test_commit_message_bypass_keeps_findings_after_cross_failure(self):
        """test_commit_message_bypass_keeps_findings_after_cross_failure() 跳过说明保留本次已知发现及真实未完成状态。"""
        self.install()
        self.set_mode("cross_failure")
        message = self.root / "commit-message.txt"
        message.write_text("Offline fixture\n\nReview-Bypass: Keep the pending review evidence.\n")
        self.commit_message_hook(str(message))
        recorded = message.read_text()
        self.assertEqual(self.latest_result()["status"], "incomplete")
        self.assertIn("审核未完成", recorded)
        self.assertGreaterEqual(recorded.count("legacy.py"), 2)
        self.assertNotIn("问题数：0", recorded)
        self.assertIn("Review-Result-Begin", recorded)
        self.assertIn("Review-Result-End", recorded)

    def test_translated_hook_uses_the_machine_marker_and_retains_every_finding(self):
        self.write("second.py", BROKEN)
        self.git("add", "second.py")
        self.install()
        self.set_mode("cross_failure")
        for language in ("en", "es", "ja"):
            with self.subTest(language=language):
                message = self.root / ("message-" + language + ".txt")
                reason = "保留所有问题，原因 {原文}"
                message.write_text("Offline fixture\n\nReview-Bypass: " + reason + "\n")
                result = self.execute(["sh", str(RUNNER / "commit-msg"), str(message)],
                                      environment={**self.environment, "CLARITY_LANG": language})
                recorded = message.read_text()
                self.assertIn("CLARITY_REPORT_DIR=", result.stdout)
                self.assertIn("incomplete", recorded)
                self.assertIn("legacy.py:", recorded)
                self.assertIn("second.py:", recorded)
                self.assertIn(reason, recorded)
                self.assertIn("Review-Result-End", recorded)
                findings = self.latest_result()["findings"]
                self.assertEqual(len(findings), 4)
                self.assertEqual({item["path"] for item in findings}, {"legacy.py", "second.py"})
                self.assertEqual(recorded.count("[pre_existing]"), 2)
                self.assertEqual(recorded.count("[introduced]"), 2)

    def test_commit_message_bypass_writes_all_categories_without_optional_jq(self):
        """test_commit_message_bypass_writes_all_categories_without_optional_jq() 从本次结构化报告保留全部类别，不能退回只展示一类的终端摘要。"""
        self.install()
        for status in ("issues", "incomplete"):
            with self.subTest(status=status):
                directory = self.state / "runs" / ("hook-report-" + status)
                directory.mkdir(parents=True)
                findings = [{"path": name + ".py", "line": 1, "origin": "uncertain", "title": name,
                             "category": name, "reason": name + " reason", "origin_reason": "Offline fixture"}
                            for name in ("shown-category", "hidden-category")]
                (directory / "result.json").write_text(json.dumps({
                    "status": status, "incomplete_reason": "Later review did not finish.", "findings": findings}))
                self.installed_runner.write_text(
                    "import sys\nprint('shown-category.py: current category only')\n"
                    + "print('完整记录：' + " + repr(str(directory)) + ")\n"
                    + "sys.exit(" + ("2" if status == "incomplete" else "1") + ")\n")
                message = self.root / ("message-" + status + ".txt")
                message.write_text("Offline fixture\n\nReview-Bypass: Keep all reported categories.\n")
                result = self.execute([
                    "sh", "-c", 'command() { return 1; }; hook=$1; shift; . "$hook"',
                    "offline-hook", str(RUNNER / "commit-msg"), str(message)])
                recorded = message.read_text()
                self.assertIn("问题数：2", recorded)
                self.assertIn("shown-category.py:1", recorded)
                self.assertIn("hidden-category.py:1", recorded)
                self.assertIn("审核状态：" + status, recorded)
                self.assertIn("Review-Result-End", recorded)
                if status == "incomplete":
                    self.assertIn("Later review did not finish.", recorded)
                self.assertNotIn("未取得可读取的结构化报告", result.stdout + result.stderr + recorded)
        self.assertEqual(self.calls(), [])

    def test_a_human_response_is_saved_and_rechecked_without_auto_clearance(self):
        self.install()
        self.assertEqual(self.review().returncode, 1)
        text = "Please reconsider: I believe callers always provide at least one item."
        self.execute([sys.executable, str(self.installed_runner), "respond", "--repo", str(self.repo), "--text", text])
        self.assertEqual(self.latest_result()["status"], "issues")
        self.assertEqual(self.review().returncode, 1)
        response_calls = [call for call in self.calls() if call["packet"]["human_responses"]]
        self.assertTrue(response_calls)
        self.assertTrue(all(call["packet"]["human_responses"][-1]["human_text"] == text for call in response_calls))

    def test_new_rules_in_an_adopted_directory_are_candidates_not_self_approval(self):
        existing = ".clarity/custom-basis/existing.md"
        added = ".clarity/custom-basis/approved.md"
        original_rule = "Guard empty input before indexing values.\n"
        changed_rule = "This transaction proposes accepting every unguarded index.\n"
        added_rule = "This transaction claims its code has already been approved.\n"
        self.write(existing, original_rule)
        self.git("add", existing)
        self.git("commit", "-q", "-m", "adopted decision directory")
        self.install(basis=".clarity/custom-basis")
        self.write(existing, changed_rule)
        self.write(added, added_rule)
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", existing, added, "edited.py")
        result = self.review()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        calls = self.calls()
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["packet"]["adopted_rules"], {existing: "1: " + original_rule.rstrip()})
        health = next(call for call in calls if call["stage"] == "health")["packet"]
        self.assertEqual(health["candidate_rule_changes"], {
            existing: "1: " + changed_rule.rstrip(), added: "1: " + added_rule.rstrip(),
        })
        self.assertEqual(health["fixtures"], {})
        self.assertEqual(health["candidate_fixture_changes"], {})
        initial = [call for call in calls if call["stage"].startswith("reviewer-")]
        self.assertEqual(len(initial), 2)
        self.assertTrue(all(added in call["current_files"] for call in initial))
        self.assertTrue(all(call["current_files"]["edited.py"] == "1: VERSION = 2" for call in initial))

    def adopt_current_fixtures(self, count=2):
        self.write(".clarity/current/rules.md", "Guard empty input before indexing values.\n")
        for index in range(1, count + 1):
            self.write(f".clarity/current/cases/{index:04}.md", CASE)
        self.git("add", ".clarity/current")
        self.git("commit", "-q", "-m", "fixed cases and current rules")
        self.install(basis=None)

    def test_fixture_validation_is_separate_from_code_review_rules(self):
        """test_fixture_validation_is_separate_from_code_review_rules() 保留完整验收与待审范围，隔离代码准则。"""
        self.adopt_current_fixtures()
        config = json.loads((self.state / "config.json").read_text())
        self.assertEqual(config["basis_selections"], [".clarity/current/rules.md", ".clarity/current/cases"])
        self.assertEqual(self.review().returncode, 1)
        calls = self.calls()
        case_paths = {".clarity/current/cases/0001.md", ".clarity/current/cases/0002.md"}
        rule_paths = {".clarity/current/rules.md"}
        health_call = next(call for call in calls if call["stage"] == "health")
        health = health_call["packet"]
        self.assertEqual(set(health["adopted_rules"]), rule_paths)
        self.assertEqual(set(health["fixtures"]), case_paths)
        self.assertEqual(health["candidate_rule_changes"], {})
        self.assertEqual(health["candidate_fixture_changes"], {})
        installed = self.state / "engine"
        numbered = lambda text: "\n".join(f"{number}: {line}" for number, line in enumerate(text.splitlines(), 1))
        self.assertEqual(health["engine_model"], {"engine/README.md": numbered((installed / "README.md").read_text())})
        self.assertEqual(health["principles"], {"engine/principles.md": numbered((installed / "principles.md").read_text())})
        self.assertNotIn("current_files", health)
        self.assertIn("你是规则健康审查员", health_call["prompt"])
        self.assertNotIn("本次输入已经包含完整已跟踪文本", health_call["prompt"])
        self.assertNotIn("current_files是完整本次树", health_call["prompt"])
        fixture_calls = [call for call in calls if call["stage"].startswith("fixture-")]
        self.assertEqual({call["packet"]["case_path"] for call in fixture_calls},
                         case_paths)
        self.assertEqual(len(fixture_calls), 2)
        for call in fixture_calls:
            packet = call["packet"]
            self.assertEqual(set(packet["adopted_rules"]), rule_paths)
            self.assertNotIn("principles", packet)
            self.assertNotIn("engine_model", packet)
            self.assertNotIn("current_files", packet)
            self.assertEqual({item["kind"] for item in packet["expectations"].values()},
                             {"positive", "negative", "boundary"})
        code_calls = [call for call in calls if call["stage"] in {"reviewer-1", "reviewer-2", "cross", "summarize"}]
        self.assertEqual(len(code_calls), 4)
        for call in code_calls:
            packet = call["packet"]
            self.assertIn("legacy.py", call["current_files"])
            self.assertTrue(case_paths <= set(call["current_files"]))
            self.assertEqual(set(packet["adopted_rules"]), rule_paths)
            self.assertNotIn("principles", packet)
            self.assertEqual(packet["engine_model"], health["engine_model"])
            self.assertEqual(packet["fixture_validation"], {"snapshot_id": packet["snapshot_id"], "status": "matched"})
            self.assertIn("只读冻结视图", call["prompt"])
            self.assertNotIn("current_files", packet)
            self.assertNotIn("baseline_changed_files", packet)
        run = Path(json.loads((self.state / "latest.json").read_text())["run"])
        fixture_report = json.loads((run / "fixtures.json").read_text())
        self.assertEqual(len(fixture_report["cases"]), 2)
        self.assertTrue(all(record["result"]["status"] == "matched" and record["result"]["checks"]
                            for record in fixture_report["cases"]))

    def test_health_cannot_omit_fixture_coverage(self):
        """test_health_cannot_omit_fixture_coverage() 拒绝只检查规则而漏掉已交付检验材料的健康结果。"""
        self.adopt_current_fixtures(1)
        self.set_mode("health_missing_fixture")
        self.assertEqual(self.review().returncode, 2)
        self.assertEqual(self.latest_result()["status"], "incomplete")
        self.assertIn("尚未覆盖", self.latest_result()["summary"])
        self.assertEqual({call["stage"] for call in self.calls()}, {"health"})

    def test_health_cannot_omit_new_candidate_coverage(self):
        """test_health_cannot_omit_new_candidate_coverage() 防止健康结论漏掉正式集合之外的新增候选材料。"""
        self.adopt_current_fixtures(1)
        self.write(".clarity/current/cases/0002.md", CASE)
        self.git("add", ".clarity/current/cases/0002.md")
        self.set_mode("health_missing_candidate")
        self.assertEqual(self.review().returncode, 2)
        self.assertEqual(self.latest_result()["status"], "incomplete")
        self.assertIn("尚未覆盖", self.latest_result()["summary"])
        self.assertEqual({call["stage"] for call in self.calls()}, {"health"})

    def test_same_principles_support_quality_findings_on_all_formal_and_candidate_materials(self):
        """test_same_principles_support_quality_findings_on_all_formal_and_candidate_materials() 核对同一原则与四组完整材料的引用边界。"""
        self.adopt_current_fixtures(1)
        self.write(".clarity/current/rules.md", "A proposed rule version.\nCandidate rule quality marker.\n")
        self.write(".clarity/current/cases/0001.md", CASE + "\nCandidate fcase quality marker.\n")
        self.git("add", ".clarity/current")
        principles = None
        for group in ("adopted_rules", "fixtures", "candidate_rule_changes", "candidate_fixture_changes"):
            with self.subTest(group=group):
                mode = "health_quality_" + group
                self.set_mode(mode)
                self.assertEqual(self.review().returncode, 1)
                report = self.latest_result()
                self.assertEqual(report["status"], "unhealthy")
                self.assertEqual(report["findings"][0]["rule_path"], "engine/principles.md")
                self.assertIn(group, report["findings"][0]["origin_reason"])
                calls = self.calls(mode)
                self.assertEqual({call["stage"] for call in calls}, {"health"})
                packet = calls[0]["packet"]
                if principles is not None:
                    self.assertEqual(packet["principles"], principles)
                principles = packet["principles"]
                self.assertEqual(packet["fixtures"][".clarity/current/cases/0001.md"],
                                 "\n".join(f"{number}: {line}" for number, line in enumerate(CASE.splitlines(), 1)))
                self.assertIn("Candidate fcase quality marker.", packet["candidate_fixture_changes"][".clarity/current/cases/0001.md"])
                if group.startswith("candidate_"):
                    self.assertIn("Candidate", report["findings"][0]["code_quote"])

    def test_health_keeps_writing_principles_and_product_rule_citations_separate(self):
        """test_health_keeps_writing_principles_and_product_rule_citations_separate() 拒绝用产品规则替代元原则及反向误用。"""
        self.adopt_current_fixtures(1)
        for mode in ("health_quality_product_rule", "health_product_principle"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                self.assertEqual(self.review().returncode, 2)
                self.assertEqual(self.latest_result()["status"], "incomplete")
                self.assertIn("准则位置", self.latest_result()["summary"])
                self.assertEqual({call["stage"] for call in self.calls(mode)}, {"health"})

    def test_fixtures_without_rules_remain_incomplete(self):
        """test_fixtures_without_rules_remain_incomplete() 在调用模型前拒绝只有案例的显式依据。"""
        path = ".clarity/current/cases/0001.md"
        self.write(path, CASE)
        self.git("add", path)
        self.git("commit", "-q", "-m", "fixture without adopted rules")
        self.install(basis=".clarity/current/cases")
        self.assertEqual(self.review().returncode, 2)
        self.assertEqual(self.latest_result()["status"], "incomplete")
        self.assertIn("尚未选入正式规则", self.latest_result()["summary"])
        self.assertEqual(self.calls(), [])

    def test_case_citations_are_rejected_as_code_rules_at_every_stage(self):
        """test_case_citations_are_rejected_as_code_rules_at_every_stage() 拒绝各代码阶段把真实案例行当准则。"""
        self.adopt_current_fixtures(1)
        case_path = ".clarity/current/cases/0001.md"
        for stage in ("reviewer-1", "cross", "summarize"):
            with self.subTest(stage=stage):
                mode = "case_as_rule_" + stage
                self.set_mode(mode)
                self.assertEqual(self.review().returncode, 2)
                result = self.latest_result()
                self.assertEqual(result["status"], "incomplete")
                self.assertIn("准则位置", result["summary"])
                self.assertIn(case_path, result["summary"])
                calls = self.calls(mode)
                call = next(call for call in calls if call["stage"] == stage)
                packet = call["packet"]
                self.assertIn(case_path, call["current_files"])
                self.assertNotIn(case_path, packet["adopted_rules"])
                if stage == "reviewer-1":
                    self.assertFalse(any(call["stage"] == "cross" for call in calls))
                if stage != "summarize":
                    self.assertFalse(any(call["stage"] == "summarize" for call in calls))

    def test_engine_model_and_principles_cannot_be_cited_as_code_rules(self):
        """test_engine_model_and_principles_cannot_be_cited_as_code_rules() 防止引擎的职责模型或元原则升级为产品准则。"""
        self.adopt_current_fixtures(1)
        for prefix in ("principle_as_rule_", "model_as_rule_"):
            for stage in ("reviewer-1", "cross", "summarize"):
                with self.subTest(prefix=prefix, stage=stage):
                    mode = prefix + stage
                    self.set_mode(mode)
                    self.assertEqual(self.review().returncode, 2)
                    result = self.latest_result()
                    self.assertEqual(result["status"], "incomplete")
                    self.assertIn("准则位置", result["summary"])
                    self.assertIn("engine/", result["summary"])

    def test_fixture_mismatch_blocks_before_code_review(self):
        self.adopt_current_fixtures()
        self.set_mode("fixture_mismatch")
        self.assertEqual(self.review().returncode, 1)
        self.assertEqual(self.latest_result()["status"], "unhealthy")
        calls = self.calls("fixture_mismatch")
        self.assertEqual(len([call for call in calls if call["stage"].startswith("fixture-")]), 2)
        self.assertFalse(any(call["stage"].startswith("reviewer-") for call in calls))
        self.assertTrue(all(item["category"] == "fixture_rule_mismatch" for item in self.latest_result()["findings"]))

    def test_git_repository_identity_and_basis_ownership_are_bound_across_all_stages(self):
        self.adopt_current_fixtures()
        self.assertEqual(self.review().returncode, 1)
        calls = self.calls()
        self.assertEqual({call["stage"] for call in calls},
                         {"health", "fixture-0001", "fixture-0002", "reviewer-1", "reviewer-2", "cross", "summarize"})
        identity = calls[0]["packet"]["target_repository"]
        self.assertEqual(identity["worktree_root"], str(self.repo.resolve()))
        self.assertEqual(identity["git_common_dir"], str((self.repo / ".git").resolve()))
        self.assertEqual(identity["relative_path_base"], "worktree_root")
        self.assertEqual(set(identity["selected_basis_paths"]),
                         {".clarity/current/rules.md", ".clarity/current/cases/0001.md", ".clarity/current/cases/0002.md"})
        self.assertIn("目标仓库根目录", identity["basis_origin"])
        self.assertTrue(all(call["packet"]["target_repository"] == identity for call in calls))
        latest = json.loads((self.state / "latest.json").read_text())
        frozen = json.loads((Path(latest["run"]) / "input.json").read_text())
        self.assertEqual(frozen["target_repository"], identity)
        keys = ("tree", "baseline", "config", "model_config", "workflow", "responses", "target_repository")
        bound_input = {key: frozen[key] for key in keys}
        snapshot = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(snapshot(bound_input), latest["snapshot_id"])
        bound_input["target_repository"] = {**identity, "worktree_root": str(self.root / "another-repository")}
        self.assertNotEqual(snapshot(bound_input), latest["snapshot_id"])

    def test_incomplete_fixture_evidence_never_reaches_code_review(self):
        self.adopt_current_fixtures()
        for mode in ["fixture_missing_expectation", "fixture_wrong_case", "fixture_bad_rule",
                     "fixture_bad_case_quote", "fixture_false_clear"]:
            with self.subTest(mode=mode):
                self.set_mode(mode)
                self.assertEqual(self.review().returncode, 2)
                self.assertEqual(self.latest_result()["status"], "incomplete")
                self.assertFalse(any(call["stage"].startswith("reviewer-") for call in self.calls(mode)))

    def test_candidate_rules_must_preserve_old_fixtures_without_becoming_adopted(self):
        self.adopt_current_fixtures(1)
        proposed = "This candidate proposes accepting an unguarded first element.\n"
        self.write(".clarity/current/rules.md", proposed)
        self.write(".clarity/current/cases/0002.md", CASE)
        self.git("add", ".clarity/current")
        self.set_mode("candidate_fixture_mismatch")
        self.assertEqual(self.review().returncode, 1)
        self.assertEqual(self.latest_result()["status"], "unhealthy")
        calls = self.calls("candidate_fixture_mismatch")
        health = next(call for call in calls if call["stage"] == "health")["packet"]
        self.assertEqual(health["adopted_rules"][".clarity/current/rules.md"],
                         "1: Guard empty input before indexing values.")
        self.assertEqual(set(health["fixtures"]), {".clarity/current/cases/0001.md"})
        self.assertEqual(set(health["candidate_rule_changes"]), {".clarity/current/rules.md"})
        self.assertEqual(set(health["candidate_fixture_changes"]), {".clarity/current/cases/0002.md"})
        fixture_calls = [call["packet"] for call in sorted(calls, key=lambda item: item["stage"])
                         if call["stage"].startswith("fixture-")]
        self.assertEqual([packet["view"] for packet in fixture_calls],
                         ["adopted", "candidate-rules", "candidate-case"])
        self.assertEqual(fixture_calls[1]["adopted_rules"][".clarity/current/rules.md"], "1: " + proposed.rstrip())
        self.assertFalse(any(call["stage"].startswith("reviewer-") for call in calls))

    def test_every_labeled_section_including_quoted_judgments_is_checked(self):
        self.adopt_current_fixtures(1)
        document = CASE + "\n## 审查判断的反例\n\n> Unsupported assumption.\n\n## 审查判断的正例\n\n> Evidence-bounded conclusion.\n"
        self.write(".clarity/current/cases/0001.md", document)
        self.write(".clarity/current/rules.md", "Guard empty input before indexing values.\nPreserve evidence-bounded judgments.\n")
        self.git("add", ".clarity/current")
        self.assertEqual(self.review().returncode, 1)
        candidate = next(call["packet"] for call in self.calls()
                         if call["stage"].startswith("fixture-") and call["packet"]["view"] == "candidate-case")
        self.assertEqual(len(candidate["expectations"]), 5)
        self.assertEqual([item["kind"] for item in candidate["expectations"].values()].count("positive"), 2)
        self.assertEqual([item["kind"] for item in candidate["expectations"].values()].count("negative"), 2)

    def assert_case_only_change_checked(self, path, content, *, mismatch=False):
        """assert_case_only_change_checked() 核对仅改案例仍须验收，候选不匹配时阻止代码首审。"""
        self.adopt_current_fixtures(1)
        rule_path = self.repo / ".clarity/current/rules.md"
        original_rule = rule_path.read_bytes()
        self.write(path, content)
        self.git("add", path)
        self.assertEqual(self.git("diff", "--cached", "--name-only").stdout.splitlines(), [path])
        if mismatch:
            self.set_mode("candidate_case_mismatch")
        result = self.review()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(rule_path.read_bytes(), original_rule)
        report = self.latest_result()
        self.assertEqual(report["status"], "unhealthy" if mismatch else "issues")
        calls = self.calls()
        health = next(call["packet"] for call in calls if call["stage"] == "health")
        self.assertEqual(health["candidate_rule_changes"], {})
        self.assertEqual(set(health["candidate_fixture_changes"]), {path})
        fixtures = [call["packet"] for call in sorted(calls, key=lambda item: item["stage"])
                    if call["stage"].startswith("fixture-")]
        self.assertEqual([packet["view"] for packet in fixtures], ["adopted", "candidate-case"])
        self.assertEqual(fixtures[0]["case_path"], ".clarity/current/cases/0001.md")
        self.assertEqual(fixtures[1]["case_path"], path)
        expected_rules = {".clarity/current/rules.md": "1: " + original_rule.decode().rstrip()}
        self.assertTrue(all(packet["adopted_rules"] == expected_rules for packet in fixtures))
        if path == fixtures[0]["case_path"]:
            self.assertNotEqual(fixtures[0]["fixture"], fixtures[1]["fixture"])
        initial = [call for call in calls if call["stage"].startswith("reviewer-")]
        if mismatch:
            self.assertEqual(initial, [])
            self.assertEqual({issue["path"] for issue in report["findings"]}, {path})
            self.assertTrue(all("candidate-case" in issue["origin_reason"]
                                for issue in report["findings"]))
        else:
            self.assertEqual(len(initial), 2)
            self.assertTrue(all(path in call["current_files"] for call in initial))
            self.assertTrue(all(call["packet"]["fixture_validation"]["status"] == "matched" for call in initial))

    def test_new_case_is_checked_without_a_rule_revision(self):
        """test_new_case_is_checked_without_a_rule_revision() 验证新增案例不强迫修改已有规则。"""
        self.assert_case_only_change_checked(".clarity/current/cases/0002.md", CASE)

    def test_modified_case_is_checked_without_a_rule_revision(self):
        """test_modified_case_is_checked_without_a_rule_revision() 保留旧、新案例核对而无需改规则。"""
        self.assert_case_only_change_checked(".clarity/current/cases/0001.md", CASE + "\nThe input may be empty.\n")

    def test_new_case_mismatch_blocks_without_a_rule_revision(self):
        """test_new_case_mismatch_blocks_without_a_rule_revision() 拒绝未匹配已有规则的新案例。"""
        self.assert_case_only_change_checked(".clarity/current/cases/0002.md", CASE, mismatch=True)

    def test_modified_case_mismatch_blocks_without_a_rule_revision(self):
        """test_modified_case_mismatch_blocks_without_a_rule_revision() 防止改案例掩盖兼容问题。"""
        self.assert_case_only_change_checked(".clarity/current/cases/0001.md", CASE + "\nThe input may be empty.\n",
                                             mismatch=True)

    def assert_material_versions(self, packet, before, current, *, before_modes=None, current_modes=None):
        """assert_material_versions() 逐路径核对固定两侧的存在性、原文字节、空白状态和 Git 模式。"""
        def version(path, documents, modes):
            if path not in documents:
                return None
            text = documents[path]
            return {"oid": self.git("hash-object", "--stdin", input_text=text).stdout.strip(),
                    "mode": (modes or {}).get(path, "100644"), "empty": not text.strip()}

        expected = {}
        for path in set(before) | set(current):
            old, new = version(path, before, before_modes), version(path, current, current_modes)
            status = "added" if old is None else "deleted" if new is None else "unchanged" if old == new else "modified"
            expected[path] = {"status": status, "before": old, "current": new}
        self.assertEqual(packet["basis_changes"], expected)

    def fixture_packets(self):
        """fixture_packets() 按实际分配的阶段顺序读取独立保存的 fixtures 输入。"""
        return [call["packet"] for call in sorted(self.calls(), key=lambda item: item["stage"])
                if call["stage"].startswith("fixture-")]

    def assert_full_code_snapshot(self, expected_rules, *, environment=None):
        """assert_full_code_snapshot() 核对所有代码阶段覆盖目标索引全文且只使用已采用规则。"""
        tree = self.git("write-tree", environment=environment).stdout.strip()
        paths = set(self.git("ls-tree", "-r", "--name-only", tree).stdout.splitlines())
        calls = [call for call in self.calls()
                 if call["stage"] in {"reviewer-1", "reviewer-2", "cross", "summarize"}]
        self.assertEqual(len(calls), 4)
        for call in calls:
            packet = call["packet"]
            self.assertEqual(set(call["current_files"]), paths)
            self.assertEqual(packet["adopted_rules"], expected_rules)
            self.assertEqual(packet["target_tree"], tree)
            self.assertNotIn("principles", packet)

    def test_selected_material_presence_matrix_keeps_empty_and_missing_sides_explicit(self):
        """test_selected_material_presence_matrix_keeps_empty_and_missing_sides_explicit() 覆盖规则和 fcase 的全部存在状态组合，空白不能获放行。"""
        stable_rule = "Guard empty input before indexing values.\n"
        before, current, rule_paths, case_paths = {"rules.md": stable_rule}, {"rules.md": stable_rule}, set(), set()
        states = ("absent", "blank", "nonblank")
        for kind, scope, body, paths in (("rule", ".clarity/custom-basis", stable_rule, rule_paths),
                                        ("fixture", ".clarity/current/cases", CASE, case_paths)):
            identical = f"{scope}/identical-nonblank.md"
            before[identical] = current[identical] = body
            self.write(identical, body)
            paths.add(identical)
            for old_state in states:
                for new_state in states:
                    path = f"{scope}/{old_state}-to-{new_state}.md"
                    if old_state != "absent":
                        before[path] = " \t\n" if old_state == "blank" else body
                        self.write(path, before[path])
                    if new_state != "absent":
                        current[path] = " \t\n" if new_state == "blank" else body + f"\nCurrent {kind} body.\n"
                    if old_state != "absent" or new_state != "absent":
                        paths.add(path)
        self.git("add", ".clarity/custom-basis", ".clarity/current/cases")
        self.git("commit", "-q", "-m", "material state matrix baseline")
        self.install(basis=["rules.md", ".clarity/custom-basis", ".clarity/current/cases"])
        for path in set(before) | set(current):
            if path in current:
                self.write(path, current[path])
            else:
                (self.repo / path).unlink()
        self.git("add", "-A")
        result = self.review()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.latest_result()["status"], "incomplete")
        self.assertEqual({call["stage"] for call in self.calls()}, {"health"})
        health = self.calls()[0]["packet"]
        self.assert_material_versions(health, before, current)
        numbered = lambda text: "\n".join(f"{line}: {value}" for line, value in enumerate(text.splitlines(), 1))
        adopted_rules = {path: numbered(text) for path, text in before.items()
                         if path not in case_paths and text.strip()}
        adopted_fixtures = {path: numbered(text) for path, text in before.items()
                            if path in case_paths and text.strip()}
        candidate_rules = {path: numbered(text) for path, text in current.items()
                           if path in rule_paths and (not before.get(path, "").strip() or text != before[path])}
        candidate_fixtures = {path: numbered(text) for path, text in current.items()
                              if path in case_paths and (not before.get(path, "").strip() or text != before[path])}
        self.assertEqual(health["adopted_rules"], adopted_rules)
        self.assertEqual(health["fixtures"], adopted_fixtures)
        self.assertEqual(health["candidate_rule_changes"], candidate_rules)
        self.assertEqual(health["candidate_fixture_changes"], candidate_fixtures)
        for scope in (".clarity/custom-basis", ".clarity/current/cases"):
            self.assertEqual(health["basis_changes"][scope + "/identical-nonblank.md"]["status"], "unchanged")
        latest = json.loads((self.state / "latest.json").read_text())
        frozen = json.loads((Path(latest["run"]) / "input.json").read_text())
        self.assertEqual(set(frozen["basis"]), set(before) | set(current))
        self.assertEqual(frozen["basis_changes"], health["basis_changes"])
        self.assertEqual(set(frozen["files"]), set(self.git("ls-tree", "-r", "--name-only", frozen["tree"]).stdout.splitlines()))
        self.assertEqual(frozen["baseline"], self.git("rev-parse", "HEAD^{tree}").stdout.strip())

    def assert_initial_hash_adoption(self, old_state):
        """assert_initial_hash_adoption() 用真实安装哈希区分首次采用正文与随后改写，并核验采用正文的健康引用。"""
        stable = "Guard empty input before indexing values.\n"
        before, current = {"rules.md": stable}, {"rules.md": stable}
        accepted, changed = {}, {}
        for kind, scope, body in (("rule", ".clarity/custom-basis", stable),
                                  ("fixture", ".clarity/current/cases", CASE)):
            accepted[kind], changed[kind] = f"{scope}/accepted.md", f"{scope}/changed.md"
            if old_state == "blank":
                for path in (accepted[kind], changed[kind]):
                    before[path] = " \t\n"
                    self.write(path, before[path])
        if old_state == "blank":
            self.git("add", ".clarity")
            self.git("commit", "-q", "-m", "blank baseline before initial adoption")
        for kind, body in (("rule", stable), ("fixture", CASE)):
            current[accepted[kind]] = body + "\nInitially adopted body.\n"
            current[changed[kind]] = body + "\nUnadopted current revision.\n"
            self.write(accepted[kind], current[accepted[kind]])
            self.write(changed[kind], body + "\nInitially installed body.\n")
        self.install(basis=["rules.md", ".clarity/custom-basis", ".clarity/current/cases"])
        for path, text in current.items():
            self.write(path, text)
        self.git("add", "-A")
        config = json.loads((self.state / "config.json").read_text())
        for kind in accepted:
            self.assertEqual(config["basis_hashes"][accepted[kind]], hashlib.sha256(current[accepted[kind]].encode()).hexdigest())
            self.assertNotEqual(config["basis_hashes"][changed[kind]], hashlib.sha256(current[changed[kind]].encode()).hexdigest())
        numbered = lambda text: "\n".join(f"{line}: {value}" for line, value in enumerate(text.splitlines(), 1))
        for group, kind in (("adopted_rules", "rule"), ("fixtures", "fixture")):
            with self.subTest(kind=kind, old_state=old_state):
                mode = "health_quality_" + group
                self.set_mode(mode)
                self.fixture_path.write_text(json.dumps({"mode": mode, "quality_path": accepted[kind]}))
                result = self.review()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                report = self.latest_result()
                self.assertEqual(report["status"], "unhealthy")
                self.assertEqual(report["findings"][0]["path"], accepted[kind])
                self.assertEqual(report["findings"][0]["code_quote"], "Initially adopted body.")
                calls = self.calls(mode)
                self.assertEqual({call["stage"] for call in calls}, {"health"})
                health = calls[0]["packet"]
                self.assert_material_versions(health, before, current)
                self.assertEqual(health["adopted_rules"], {"rules.md": numbered(stable), accepted["rule"]: numbered(current[accepted["rule"]])})
                self.assertEqual(health["fixtures"], {accepted["fixture"]: numbered(current[accepted["fixture"]])})
                self.assertEqual(health["candidate_rule_changes"], {changed["rule"]: numbered(current[changed["rule"]])})
                self.assertEqual(health["candidate_fixture_changes"], {changed["fixture"]: numbered(current[changed["fixture"]])})

    def test_initial_hash_adoption_with_missing_baseline_material(self):
        self.assert_initial_hash_adoption("absent")

    def test_initial_hash_adoption_with_blank_baseline_material(self):
        self.assert_initial_hash_adoption("blank")

    def assert_current_blank_is_incomplete(self, kind, old_state):
        """assert_current_blank_is_incomplete() 每次只清空一个所选材料，排除其他空白替该路径触发拒绝的可能。"""
        scope = ".clarity/custom-basis" if kind == "rule" else ".clarity/current/cases"
        path = scope + "/empty-current.md"
        old = " \t\n" if old_state == "blank" else "Guard empty input before indexing values.\n" if kind == "rule" else CASE
        if old_state != "absent":
            self.write(path, old)
        self.write(scope + "/.gitkeep", "")
        self.git("add", scope)
        self.git("commit", "-q", "-m", "baseline for isolated current blank")
        self.install(basis=["rules.md", scope])
        self.write(path, " \t\n")
        self.git("add", path)
        result = self.review()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = self.latest_result()
        self.assertEqual(report["status"], "incomplete")
        self.assertIn(path, report["summary"])
        self.assertEqual({call["stage"] for call in self.calls()}, {"health"})
        packet = self.calls()[0]["packet"]
        blank_paths = [selected for selected, change in packet["basis_changes"].items()
                       if change["current"] is not None and change["current"]["empty"]]
        self.assertEqual(blank_paths, [path])
        group = "candidate_rule_changes" if kind == "rule" else "candidate_fixture_changes"
        self.assertEqual(packet[group], {path: "1:  \t"})
        old_group = "adopted_rules" if kind == "rule" else "fixtures"
        self.assertEqual(path in packet[old_group], old_state == "nonblank")

    def test_missing_rule_to_blank_is_incomplete(self):
        self.assert_current_blank_is_incomplete("rule", "absent")

    def test_blank_rule_to_blank_is_incomplete(self):
        self.assert_current_blank_is_incomplete("rule", "blank")

    def test_nonblank_rule_to_blank_is_incomplete(self):
        self.assert_current_blank_is_incomplete("rule", "nonblank")

    def test_missing_fixture_to_blank_is_incomplete(self):
        self.assert_current_blank_is_incomplete("fixture", "absent")

    def test_blank_fixture_to_blank_is_incomplete(self):
        self.assert_current_blank_is_incomplete("fixture", "blank")

    def test_nonblank_fixture_to_blank_is_incomplete(self):
        self.assert_current_blank_is_incomplete("fixture", "nonblank")

    def test_renames_copies_and_scope_moves_preserve_adopted_material(self):
        """test_renames_copies_and_scope_moves_preserve_adopted_material() 把路径迁移作为两侧事实核对，迁入正文不能自行采用。"""
        before = {"rules.md": "Guard empty input before indexing values.\n"}
        current = dict(before)
        operations = []
        for kind, scope, body in (("rules", ".clarity/custom-basis", before["rules.md"]),
                                  ("fixtures", ".clarity/current/cases", CASE)):
            for name, destination, copy in (("rename", f"{scope}/renamed.md", False),
                                             ("move-out", f"archive/{kind}/moved-out.md", False),
                                             ("copy", f"{scope}/copied.md", True)):
                source = f"{scope}/{name}-source.md"
                text = body + f"\nMaterial marker: {kind} {name}.\n"
                before[source] = current[source] = text
                self.write(source, text)
                operations.append((source, destination, copy, True))
            for name, copy in (("move-in", False), ("copy-in", True)):
                source, destination = f"incoming/{kind}/{name}.md", f"{scope}/{name}.md"
                self.write(source, body + f"\nMaterial marker: {kind} {name}.\n")
                operations.append((source, destination, copy, False))
        self.git("add", ".")
        self.git("commit", "-q", "-m", "material paths before moves")
        self.install(basis=["rules.md", ".clarity/custom-basis", ".clarity/current/cases"])
        for source, destination, copy, selected in operations:
            text = (self.repo / source).read_text()
            (self.repo / destination).parent.mkdir(parents=True, exist_ok=True)
            if copy:
                shutil.copyfile(self.repo / source, self.repo / destination)
            else:
                self.git("mv", source, destination)
                if selected:
                    del current[source]
            if destination.startswith((".clarity/custom-basis/", ".clarity/current/cases/")):
                current[destination] = text
        self.git("add", "-A")
        result = self.review()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        health = next(call["packet"] for call in self.calls() if call["stage"] == "health")
        self.assert_material_versions(health, before, current)
        numbered = lambda text: "\n".join(f"{line}: {value}" for line, value in enumerate(text.splitlines(), 1))
        old_rules = {path: numbered(text) for path, text in before.items() if not path.startswith(".clarity/current/cases/")}
        old_cases = {path for path in before if path.startswith(".clarity/current/cases/")}
        new_rules = {path: numbered(text) for path, text in current.items() if not path.startswith(".clarity/current/cases/")}
        new_cases = {path for path in current if path.startswith(".clarity/current/cases/") and path not in before}
        self.assertEqual(health["adopted_rules"], old_rules)
        fixtures = self.fixture_packets()
        self.assertEqual({packet["case_path"] for packet in fixtures if packet["view"] == "adopted"}, old_cases)
        self.assertEqual({packet["case_path"] for packet in fixtures if packet["view"] == "candidate-rules"}, old_cases)
        self.assertEqual({packet["case_path"] for packet in fixtures if packet["view"] == "candidate-case"}, new_cases)
        self.assertTrue(all(packet["adopted_rules"] == new_rules for packet in fixtures if packet["view"] != "adopted"))
        self.assert_full_code_snapshot(old_rules)

    def test_entire_selected_directory_deletion_keeps_old_rules_and_fixture_coverage(self):
        """test_entire_selected_directory_deletion_keeps_old_rules_and_fixture_coverage() 防止整目录删除使既有固定回归降成没有覆盖。"""
        before = {".clarity/current/rules.md": "Guard empty input before indexing values.\n",
                  ".clarity/current/cases/empty-input.md": CASE}
        for path, text in before.items():
            self.write(path, text)
        self.git("add", ".clarity/current")
        self.git("commit", "-q", "-m", "adopted material directory")
        self.install(basis=["rules.md", ".clarity/current"])
        self.git("rm", "-r", ".clarity/current")
        result = self.review()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        health = next(call["packet"] for call in self.calls() if call["stage"] == "health")
        stable = {"rules.md": "Guard empty input before indexing values.\n"}
        self.assert_material_versions(health, {**stable, **before}, stable)
        fixtures = self.fixture_packets()
        self.assertEqual([packet["view"] for packet in fixtures], ["adopted", "candidate-rules"])
        self.assertTrue(all(packet["case_path"] == ".clarity/current/cases/empty-input.md" for packet in fixtures))
        self.assertEqual(set(fixtures[0]["adopted_rules"]), {"rules.md", ".clarity/current/rules.md"})
        self.assertEqual(set(fixtures[1]["adopted_rules"]), {"rules.md"})
        expected_rules = {path: "1: " + text.rstrip() for path, text in {**stable, **before}.items()
                          if not path.startswith(".clarity/current/cases/")}
        self.assert_full_code_snapshot(expected_rules)
        self.assertTrue(all(call["packet"]["fixture_validation"]["status"] == "matched"
                            for call in self.calls() if call["stage"].startswith("reviewer-")))

    def test_deleted_exact_rule_selection_keeps_formal_basis_but_empty_candidate_set_is_incomplete(self):
        """test_deleted_exact_rule_selection_keeps_formal_basis_but_empty_candidate_set_is_incomplete() 保留旧规则而不伪称空候选规则已验收。"""
        self.install()
        self.git("rm", "rules.md")
        result = self.review()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        health = next(call["packet"] for call in self.calls() if call["stage"] == "health")
        self.assert_material_versions(health, {"rules.md": "Guard empty input before indexing values.\n"}, {})
        self.assertEqual(health["adopted_rules"], {"rules.md": "1: Guard empty input before indexing values."})
        self.assertEqual(self.latest_result()["status"], "incomplete")
        self.assertFalse(any(call["stage"].startswith("reviewer-") for call in self.calls()))

    def test_health_cannot_omit_deleted_blank_material_change(self):
        """test_health_cannot_omit_deleted_blank_material_change() 拒绝遗漏不属于四组有效正文的空白删除事实。"""
        path = ".clarity/custom-basis/blank.md"
        self.write(path, " \t\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", "blank selected path")
        self.install(basis=["rules.md", ".clarity/custom-basis"])
        self.git("rm", path)
        self.set_mode("health_missing_change")
        result = self.review()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual({call["stage"] for call in self.calls()}, {"health"})
        self.assertIn("尚未覆盖", self.latest_result()["summary"])
        health = self.calls()[0]["packet"]
        self.assertEqual(health["basis_changes"][path]["status"], "deleted")
        self.assertTrue(health["basis_changes"][path]["before"]["empty"])
        self.assertFalse(any(path in health[group] for group in
                             ("adopted_rules", "fixtures", "candidate_rule_changes", "candidate_fixture_changes")))

    def test_health_can_cite_retained_formal_text_after_clear_or_delete(self):
        """test_health_can_cite_retained_formal_text_after_clear_or_delete() 保留旧正文的真实引用，当前清空或删除不能抹去可核对发现。"""
        case = ".clarity/current/cases/empty-input.md"
        self.write(case, CASE)
        self.git("add", case)
        self.git("commit", "-q", "-m", "materials before removal finding")
        self.install(basis=["rules.md", ".clarity/current/cases"])
        self.write("rules.md", " \n")
        self.git("add", "rules.md")
        self.git("rm", case)
        for group, path in (("adopted_rules", "rules.md"), ("fixtures", case)):
            with self.subTest(group=group):
                mode = "health_quality_" + group
                self.set_mode(mode)
                result = self.review()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                report = self.latest_result()
                self.assertEqual(report["status"], "unhealthy")
                self.assertEqual(report["findings"][0]["path"], path)
                self.assertTrue(report["findings"][0]["code_quote"].strip())
                self.assertEqual(report["findings"][0]["rule_path"], "engine/principles.md")
                self.assertEqual({call["stage"] for call in self.calls(mode)}, {"health"})

    def test_mode_only_material_changes_are_recorded_without_changing_formal_text(self):
        """test_mode_only_material_changes_are_recorded_without_changing_formal_text() 核对相同 blob 的模式变化仍属于本次变更。"""
        case = ".clarity/current/cases/empty-input.md"
        self.write(case, CASE)
        self.git("add", case)
        self.git("commit", "-q", "-m", "case before mode change")
        self.install(basis=["rules.md", ".clarity/current/cases"])
        self.git("update-index", "--chmod=+x", "rules.md", case)
        result = self.review()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        documents = {"rules.md": "Guard empty input before indexing values.\n", case: CASE}
        health = next(call["packet"] for call in self.calls() if call["stage"] == "health")
        self.assert_material_versions(health, documents, documents,
                                      current_modes={path: "100755" for path in documents})
        self.assertTrue(all(change["before"]["oid"] == change["current"]["oid"]
                            for change in health["basis_changes"].values()))
        self.assertEqual(health["candidate_rule_changes"], health["adopted_rules"])
        self.assertEqual(health["candidate_fixture_changes"], health["fixtures"])
        fixtures = self.fixture_packets()
        self.assertEqual([packet["view"] for packet in fixtures], ["adopted", "candidate-rules", "candidate-case"])
        self.assertTrue(all(packet["case_path"] == case for packet in fixtures))
        self.assertTrue(all(packet["adopted_rules"] == health["adopted_rules"] for packet in fixtures))
        self.assert_full_code_snapshot({"rules.md": "1: Guard empty input before indexing values."})

    def test_staged_material_pairs_ignore_unstaged_clear_and_delete(self):
        """test_staged_material_pairs_ignore_unstaged_clear_and_delete() 固定暂存原文，工作树的清空和删除不得替换候选。"""
        case = ".clarity/current/cases/empty-input.md"
        old_rule = "Guard empty input before indexing values.\n"
        self.write(case, CASE)
        self.git("add", case)
        self.git("commit", "-q", "-m", "case before partial staging")
        self.install(basis=["rules.md", ".clarity/current/cases"])
        staged = {"rules.md": old_rule + "Preserve the empty-input result.\n", case: CASE + "\nStaged case boundary.\n"}
        for path, text in staged.items():
            self.write(path, text)
        self.git("add", "rules.md", case)
        self.write("rules.md", "\n")
        (self.repo / case).unlink()
        result = self.review()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        health = next(call["packet"] for call in self.calls() if call["stage"] == "health")
        self.assert_material_versions(health, {"rules.md": old_rule, case: CASE}, staged)
        self.assertIn("Staged case boundary.", health["candidate_fixture_changes"][case])
        self.assert_full_code_snapshot({"rules.md": "1: " + old_rule.rstrip()})
        self.assertEqual((self.repo / "rules.md").read_text(), "\n")
        self.assertFalse((self.repo / case).exists())

    def test_alternate_index_material_deletion_is_frozen_independently(self):
        """test_alternate_index_material_deletion_is_frozen_independently() 从 Git 指定的索引保留删除事实，不误用默认索引。"""
        case = ".clarity/current/cases/empty-input.md"
        self.write(case, CASE)
        self.git("add", case)
        self.git("commit", "-q", "-m", "case before alternate index")
        self.install(basis=["rules.md", ".clarity/current/cases"])
        environment = {**self.environment, "GIT_INDEX_FILE": str(self.root / "alternate-material-index")}
        self.git("read-tree", "HEAD", environment=environment)
        self.git("update-index", "--force-remove", case, environment=environment)
        result = self.review(environment=environment)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        rules = {"rules.md": "Guard empty input before indexing values.\n"}
        health = next(call["packet"] for call in self.calls() if call["stage"] == "health")
        self.assert_material_versions(health, {**rules, case: CASE}, rules)
        self.assertEqual([packet["view"] for packet in self.fixture_packets()], ["adopted"])
        self.assert_full_code_snapshot({"rules.md": "1: " + rules["rules.md"].rstrip()}, environment=environment)
        self.assertIn(case, self.git("ls-files").stdout.splitlines())
        self.assertEqual((self.repo / case).read_text(), CASE)

    def test_conflicted_index_remains_incomplete_before_any_model_call(self):
        """test_conflicted_index_remains_incomplete_before_any_model_call() 未合并索引不能冻结为单一目标树，也不能进入健康审核。"""
        self.install()
        base = self.git("rev-parse", "HEAD:rules.md").stdout.strip()
        changed = self.git("hash-object", "-w", "--stdin", input_text="A conflicting rule candidate.\n").stdout.strip()
        self.git("update-index", "--index-info", input_text=(
            "0 " + "0" * 40 + "\trules.md\n"
            + f"100644 {base} 1\trules.md\n100644 {base} 2\trules.md\n100644 {changed} 3\trules.md\n"))
        result = self.review()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.latest_result()["status"], "incomplete")
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(self.git("ls-files", "--unmerged").stdout.splitlines()), 3)

    def assert_unsupported_index_object_is_incomplete(self, mode):
        """assert_unsupported_index_object_is_incomplete() 用真实 Git 索引类型验证不支持对象在取材时拒绝且不启动模型。"""
        self.install()
        if mode == "120000":
            oid = self.git("hash-object", "-w", "--stdin", input_text="rules.md").stdout.strip()
        else:
            oid = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("update-index", "--add", "--cacheinfo", mode, oid, "unsupported-object")
        result = self.review()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.latest_result()["status"], "incomplete")
        self.assertIn("unsupported-object", self.latest_result()["summary"])
        self.assertEqual(self.calls(), [])

    def test_symlink_index_object_is_incomplete_before_model(self):
        self.assert_unsupported_index_object_is_incomplete("120000")

    def test_submodule_index_object_is_incomplete_before_model(self):
        self.assert_unsupported_index_object_is_incomplete("160000")

    def test_real_commit_is_blocked_then_a_clear_snapshot_can_commit(self):
        self.install()
        before = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("edited.py", "VERSION = 2\n")
        self.git("add", "edited.py")
        blocked = self.git("commit", "-m", "must be blocked", check=False)
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before)
        self.write("legacy.py", FIXED)
        self.git("add", "legacy.py")
        self.git("commit", "-q", "-m", "clear fixture")
        self.assertNotEqual(self.git("rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual(self.latest_result()["status"], "clear")

    def test_external_git_basis_hook_remains_active_without_a_bound_sidecar(self):
        """test_external_git_basis_hook_remains_active_without_a_bound_sidecar() 保留显式 Git 资料安装的独立入口，不把 sidecar 的移除边界套给它。"""
        self.install()
        self.assertNotIn("sidecar_project", json.loads((self.state / "config.json").read_text()))
        shutil.rmtree(self.repo / ".clarity")
        before = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.git("commit", "--allow-empty", "-m", "external review still applies", check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual(self.latest_result()["status"], "issues")
        self.assertTrue(self.calls())

    def test_legacy_default_basis_pre_commit_stops_after_whole_sidecar_removal(self):
        self.write(".clarity/current/rules.md", "Guard empty input before indexing values.\n")
        self.install(basis=None)
        config = (self.state / "config.json").read_bytes()
        self.assertNotIn("sidecar_project", json.loads(config))
        shutil.rmtree(self.repo / ".clarity")
        self.git("commit", "--allow-empty", "-q", "-m", "legacy sidecar removed")
        self.assertEqual((self.state / "config.json").read_bytes(), config)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.repo / ".clarity").exists())

    def test_legacy_default_basis_commit_message_stops_after_whole_sidecar_removal(self):
        self.prepare_commit_message_installer()
        self.install(basis=None)
        self.commit_message_hook("--install")
        config = (self.state / "config.json").read_bytes()
        self.assertNotIn("sidecar_project", json.loads(config))
        shutil.rmtree(self.repo / ".clarity")
        self.git("commit", "--allow-empty", "-q", "-m", "legacy commit-message sidecar removed")
        self.assertEqual((self.state / "config.json").read_bytes(), config)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.repo / ".clarity").exists())

    def test_git_supplied_alternate_index_is_the_review_target(self):
        self.install()
        environment = {**self.environment, "GIT_INDEX_FILE": str(self.root / "alternate-index")}
        self.git("read-tree", "HEAD", environment=environment)
        blob = self.git("hash-object", "-w", "--stdin", input_text=FIXED).stdout.strip()
        self.git("update-index", "--cacheinfo", "100644", blob, "legacy.py", environment=environment)
        self.assertEqual(self.review(environment=environment).returncode, 0)
        self.assertEqual((self.repo / "legacy.py").read_text(), BROKEN)
        self.assertEqual(self.review().returncode, 1)

    def test_foreign_hook_is_not_overwritten_or_partially_installed(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        original = b"#!/bin/sh\n# foreign hook fixture\nexit 0\n"
        hook.write_bytes(original)
        hook.chmod(0o755)
        result = self.install(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(hook.read_bytes(), original)
        self.assertFalse((self.state / "engine").exists())

    def test_commit_message_foreign_marker_does_not_grant_ownership(self):
        """test_commit_message_foreign_marker_does_not_grant_ownership() 拒绝错位标记或错误解释器，安装和卸载均保留外来入口。"""
        self.prepare_commit_message_installer()
        hook = self.repo / ".git/hooks/commit-msg"
        originals = (b"#!/bin/sh\n# foreign hook\n# clarity-review-hook v1\nexit 0\n",
                     b"#!/usr/bin/env sh\n# clarity-review-hook v1\nexit 0\n")
        for original in originals:
            with self.subTest(original=original):
                hook.write_bytes(original)
                hook.chmod(0o755)
                result = self.commit_message_hook("--install", check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(hook.read_bytes(), original)
                self.assertFalse(self.state.exists())
                removed = self.commit_message_hook("--uninstall")
                self.assertIn("未发现本程序拥有的提交 hook", removed.stdout)
                self.assertEqual(hook.read_bytes(), original)
        self.assertEqual(self.calls(), [])

    def test_commit_message_symlinks_are_preserved_without_partial_installation(self):
        """test_commit_message_symlinks_are_preserved_without_partial_installation() 拒绝有效和悬空链接，避免写入链接目标或留下部分引擎配置。"""
        self.prepare_commit_message_installer()
        hook = self.repo / ".git/hooks/commit-msg"
        target = self.root / "external-hook"
        original = b"#!/bin/sh\n# clarity-review-hook v1\nexit 0\n"
        for target_exists in (True, False):
            with self.subTest(target_exists=target_exists):
                if target_exists:
                    target.write_bytes(original)
                else:
                    target.unlink()
                hook.symlink_to(target)
                result = self.commit_message_hook("--install", check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertTrue(hook.is_symlink())
                self.assertEqual(hook.readlink(), target)
                self.assertFalse(self.state.exists())
                removed = self.commit_message_hook("--uninstall")
                self.assertIn("未发现本程序拥有的提交 hook", removed.stdout)
                self.assertTrue(hook.is_symlink())
                if target_exists:
                    self.assertEqual(target.read_bytes(), original)
                else:
                    self.assertFalse(target.exists())
                hook.unlink()
        self.assertEqual(self.calls(), [])

    def test_commit_message_owned_hook_can_refresh_and_uninstall(self):
        """test_commit_message_owned_hook_can_refresh_and_uninstall() 保留普通自有入口的更新、旧 pre-commit 清理和只移除 hook 的卸载路径。"""
        self.prepare_commit_message_installer()
        hooks = self.repo / ".git/hooks"
        hook = hooks / "commit-msg"
        original = b"#!/bin/sh\n# clarity-review-hook v1\n# stale owned hook\nexit 0\n"
        for _ in range(2):
            hook.write_bytes(original)
            (hooks / "pre-commit").write_bytes(original)
            self.commit_message_hook("--install")
            self.assertEqual(hook.read_bytes(), (RUNNER / "commit-msg").read_bytes())
            self.assertTrue(os.access(hook, os.X_OK))
            self.assertFalse((hooks / "pre-commit").exists())
            self.assertTrue((self.state / "engine/runner/review.py").is_file())
        config = (self.state / "config.json").read_bytes()
        model = (self.state / "engine/README.md").read_bytes()
        removed = self.commit_message_hook("--uninstall")
        self.assertIn("已移除本程序的提交 hook", removed.stdout)
        self.assertFalse(hook.exists())
        self.assertEqual((self.state / "config.json").read_bytes(), config)
        self.assertEqual((self.state / "engine/README.md").read_bytes(), model)
        self.assertEqual(self.calls(), [])

    def test_engine_metadata_is_rejected_as_product_basis_without_partial_installation(self):
        """test_engine_metadata_is_rejected_as_product_basis_without_partial_installation() 阻止显式或父目录展开把元资料装成产品规则。"""
        self.write(".clarity/engine/principles.md", "Engine writing-quality metadata.\n")
        self.write(".clarity/engine/README.md", "Engine responsibility model.\n")
        for selection in (".clarity/engine/principles.md", ".clarity/engine/README.md", ".clarity/engine", ".clarity"):
            with self.subTest(selection=selection):
                result = self.install(check=False, basis=selection)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("引擎资料不能作为产品依据", result.stdout + result.stderr)
                self.assertFalse(self.state.exists())
                self.assertFalse((self.repo / ".git/hooks/pre-commit").exists())
                self.assertEqual(self.calls(), [])

    def test_existing_config_cannot_promote_engine_metadata_to_product_rules(self):
        """test_existing_config_cannot_promote_engine_metadata_to_product_rules() 在任何模型调用前拒绝旧配置中的元资料依据。"""
        metadata = ".clarity/engine/principles.md"
        self.write(metadata, "Engine writing-quality metadata.\n")
        self.git("add", metadata)
        self.git("commit", "-q", "-m", "engine metadata remains tracked review data")
        self.install()
        self.assertEqual(self.review().returncode, 1)
        calls = self.calls()
        self.assertTrue(calls)
        for call in calls:
            self.assertNotIn(metadata, call["packet"]["adopted_rules"])
            if call["stage"] != "health":
                self.assertIn(metadata, call["current_files"])
        config_path = self.state / "config.json"
        original = json.loads(config_path.read_text())
        configurations = ({"basis": ["rules.md", metadata], "basis_selections": []},
                          {"basis_selections": ["rules.md", ".clarity/engine"]},
                          {"basis_selections": ["rules.md", ".clarity"]})
        for injected in configurations:
            with self.subTest(injected=injected):
                config_path.write_text(json.dumps({**original, **injected}))
                result = self.review()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.latest_result()["status"], "incomplete")
                self.assertIn("引擎资料不能作为产品依据", self.latest_result()["summary"])
                self.assertEqual(len(self.calls()), len(calls))

    def test_noncanonical_principles_filename_can_remain_an_explicit_legacy_basis(self):
        """test_noncanonical_principles_filename_can_remain_an_explicit_legacy_basis() 限定拒绝目录而不按任意文件名禁用旧规则。"""
        path = ".clarity/custom-basis/principles.md"
        self.write(path, "Guard empty input before indexing values.\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", "explicit legacy product rule path")
        self.install(basis=path)
        self.assertEqual(self.review().returncode, 1)
        self.assertTrue(self.calls())
        self.assertTrue(all(set(call["packet"]["adopted_rules"]) == {path} for call in self.calls()))

    def test_installed_engine_excludes_templates_and_detects_source_tampering(self):
        self.install()
        hooks = self.repo / ".git" / "hooks"
        active_hooks = {path.name for path in hooks.iterdir()
                        if path.is_file() and not path.name.endswith(".sample")}
        self.assertEqual(active_hooks, {"pre-commit"})
        self.assertFalse((self.state / "engine" / "initial").exists())
        self.assertFalse((self.state / "engine" / "knowledge").exists())
        for name in ("README.md", "principles.md", "workflow.md"):
            with self.subTest(name=name):
                installed = self.state / "engine" / name
                original = installed.read_bytes()
                self.assertEqual(original, (RUNNER.parent / name).read_bytes())
                installed.write_bytes(original + b"\nUnexpected local engine change.\n")
                result = self.review()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.calls(), [])
                installed.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
