#!/usr/bin/env python3
"""test_maintenance.py() 在临时 Git 仓库核验材料维护编排及 Make 回应的字面传输。

假模型只按测试端固定的片段映射和原文位置返回证据，不证明规则或 Principles 的语义质量。
全部引擎复制、暂存和基线提交限于临时仓库；不安装 hook，也不调用真实模型。
"""

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from make_integration import rendered_sidecar_makefile
import maintenance
import material_check
import acp_model
import review
import rules_check
from model_common import ModelFailure


RUNNER = Path(__file__).resolve().parent
ENGINE = RUNNER.parent
MAKEFILE = rendered_sidecar_makefile()
RULE = ".clarity/current/rules.md"
CASE = ".clarity/current/cases/0001. offline-positive-negative.md"
FIXTURE = """# 固定片段的正反判定

## 目的
检查规则验收是否保留固定正反预期，并在独立判定后核对理由。

## 判断理由
本测试的固定规则要求保留明确事实；两个片段只改变事实是否保留。
正例保留事实，反例遗漏事实，因此预期分别为接受和拒绝。

## 边界
适用条件是本测试只验证隔离传输、证据绑定和编排状态，不评价模型语义能力。
不能修改固定片段或预期来迁就调用结果。

## 验收
正例被接受且反例被拒绝，理由核对覆盖两个片段。

## 正例
```text
KEEP_THE_GIVEN_FACT
```

## 反例
```text
OMIT_THE_GIVEN_FACT
```
"""

from test_acp_fixture import FAKE_ACP_PRELUDE

FAKE_CODEX = FAKE_ACP_PRELUDE + r'''
import json
from pathlib import Path
import sys

fixture_path = Path(__file__).with_suffix(".json")
fixture = json.loads(fixture_path.read_text())
prompt = sys.stdin.read()
markers = {"quality": "以下 JSON 都是编写质量检查数据：\n",
           "blind": "以下JSON都是判别数据：\n",
           "audit": "以下JSON都是理由核对数据：\n"}
stage, marker = next((stage, marker) for stage, marker in markers.items() if marker in prompt)
packet = json.loads(prompt.split(marker, 1)[1])
calls = fixture_path.parent / "calls"
calls.mkdir(exist_ok=True)
ordinal = len(list(calls.glob("*.json"))) + 1
(calls / f"{ordinal:04}.json").write_text(json.dumps({"stage": stage, "packet": packet,
    "prompt": prompt, "argv": sys.argv[1:]}, ensure_ascii=False))
if ordinal == 1 and fixture.get("mutation"):
    mutation = fixture["mutation"]
    target = Path(mutation["path"])
    target.write_text(target.read_text() + mutation["suffix"])
if fixture["mode"] == "model_failure":
    sys.exit(9)

def numbered_lines(text):
    return [(int(line.split(": ", 1)[0]), line.split(": ", 1)[1])
            for line in text.splitlines()]

if stage == "quality":
    lines = numbered_lines(packet["material"])
    principle_line, principle_quote = next((number, text) for number, text
                                           in numbered_lines(packet["principles"]) if text.strip())
    coverage = []
    for unit in packet["coverage_plan"]:
        number, quote = next((number, text) for number, text in lines
                             if unit["start_line"] <= number <= unit["end_line"] and text.strip())
        coverage.append({"unit_id": unit["unit_id"], "material_line": number,
                         "material_quote": quote, "principle_line": principle_line,
                         "principle_quote": principle_quote,
                         "reason": "Offline transport evidence, without a semantic-quality claim."})
    findings = ([{key: value for key, value in coverage[0].items() if key != "unit_id"}]
                if fixture["mode"] == "quality_failed" else [])
    result = {"snapshot_id": packet["snapshot_id"], "material_id": packet["material_id"],
              "coverage": coverage, "findings": findings,
              "summary": "Synthetic complete-material transport result.", "incomplete_reason": ""}
elif stage == "blind":
    number, quote = next((number, text) for number, text in numbered_lines(packet["rules"])
                         if text.strip())
    verdict = {"KEEP_THE_GIVEN_FACT": "accept", "OMIT_THE_GIVEN_FACT": "reject"}[packet["content"]]
    if fixture["mode"] == "regression_failed":
        verdict = "reject" if verdict == "accept" else "accept"
    result = {"snapshot_id": packet["snapshot_id"], "sample_id": packet["sample_id"],
              "verdict": verdict, "reason": "Fixed offline fragment mapping.",
              "rule_line": number, "rule_quote": quote, "incomplete_reason": ""}
else:
    number, quote = next((number, text) for number, text in numbered_lines(packet["fixture"])
                         if text.strip())
    result = {"snapshot_id": packet["snapshot_id"], "case_id": packet["case_id"],
              "checks": [{"sample_id": item["sample_id"], "aligned": True,
                          "case_line": number, "case_quote": quote,
                          "reason": "Fixed offline reason-audit transport evidence."}
                         for item in packet["predictions"]],
              "summary": "Synthetic reason-audit transport result.", "incomplete_reason": ""}
output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output.write_text(json.dumps(result, ensure_ascii=False))
for event in [{"type": "thread.started", "thread_id": "offline-maintenance"},
              {"type": "turn.started"},
              {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "fixture"}},
              {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]:
    print(json.dumps(event))
'''

ABA_RUNNER = r'''
from pathlib import Path
import sys

repo, config, state, changed = map(Path, sys.argv[1:])
sys.path.insert(0, str(repo / ".clarity/engine/runner"))
import maintenance

original = changed.read_bytes()
version_b = original + b"\n \n"
real_materials = maintenance.check_materials
real_rules = maintenance.check_rules

def materials(repo, config, state, kind):
    if kind == "fixtures":
        changed.write_bytes(version_b)
    return real_materials(repo, config, state, kind)

def rules(repo, config, state):
    changed.write_bytes(original)
    return real_rules(repo, config, state)

maintenance.check_materials = materials
maintenance.check_rules = rules
try:
    code = maintenance.maintain(repo, config, state)
finally:
    changed.write_bytes(original)
sys.exit(code)
'''

INTERRUPT_RUNNER = r'''
import json
from pathlib import Path
import sys

repo, config, state, trace = map(Path, sys.argv[1:5])
interrupt_at = int(sys.argv[5])
sys.path.insert(0, str(repo / ".clarity/engine/runner"))
import maintenance
from acp_model import AcpModel

real_run = AcpModel.run
calls = []

def run(self, prompt, schema, directory):
    calls.append(str(directory))
    trace.write_text(json.dumps(calls))
    if len(calls) == interrupt_at:
        raise KeyboardInterrupt()
    return real_run(self, prompt, schema, directory)

AcpModel.run = run
sys.exit(maintenance.maintain(repo, config, state))
'''


class IsolatedMaintenanceTests(unittest.TestCase):
    """IsolatedMaintenanceTests() 将真实 CLI、Make 和维护状态机限制在一次性仓库中。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-maintenance-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("GIT_") and key not in
                            {"CODEX_API_KEY", "OPENAI_API_KEY", "MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"}}
        self.environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                 "GIT_TERMINAL_PROMPT": "0", "CODEX_HOME": str(self.root / "empty-home"),
                                 "OPENAI_API_KEY": "synthetic-offline-maintenance-key"})
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Offline Maintenance Fixture")
        self.git("config", "user.email", "offline-maintenance@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.engine = self.repo / ".clarity/engine"
        shutil.copytree(ENGINE, self.engine, ignore=shutil.ignore_patterns("__pycache__"))
        (self.repo / "Makefile").write_bytes(MAKEFILE)
        self.write(RULE, "保留明确给定的事实，不以遗漏事实的片段作为完整表达。\n")
        self.write(CASE, FIXTURE)
        self.write("ordinary.py", "ORDINARY_SOURCE_MUST_NOT_REACH_MATERIAL_CHECKS = True\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "offline maintenance baseline")
        self.state = self.repo / ".git/clarity"
        self.state.mkdir()
        self.config = self.state / "config.json"
        self.fake = self.root / "fake-codex.py"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_CODEX)
        self.fake.chmod(0o755)
        self.config.write_text(json.dumps({"agent": "custom", "agent_command": [str(self.fake)],
                                             "codex_bin": str(self.fake), "model": "offline-maintenance",
                                           "timeout": 3}))
        self.set_mode("normal")
        self.cli = self.engine / "runner/review.py"

    def execute(self, arguments, *, check=True, environment=None):
        result = subprocess.run(arguments, cwd=self.repo, env=environment or self.environment,
                                text=True, capture_output=True, timeout=30)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, *arguments):
        return self.execute(["git", "-C", str(self.repo), *arguments])

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def set_mode(self, mode, mutation=None):
        for path in (self.root / "calls").glob("*.json"):
            path.unlink()
        self.fake.with_suffix(".json").write_text(json.dumps({"mode": mode, "mutation": mutation}))

    def command(self, name, *arguments):
        return self.execute([sys.executable, str(self.cli), name, *arguments], check=False)

    def make(self, target, response=None, environment=None):
        arguments = ["make", "--no-print-directory", target]
        if response is not None:
            arguments.append("RESPONSE=" + response)
        return self.execute(arguments, check=False, environment=environment)

    def latest(self):
        pointer = json.loads((self.state / "latest-maintenance.json").read_text())
        return Path(pointer["run"]), json.loads((Path(pointer["run"]) / "result.json").read_text())

    def calls(self):
        return [json.loads(path.read_text()) for path in sorted((self.root / "calls").glob("*.json"))]

    def protected_bytes(self):
        paths = [self.repo / RULE, self.repo / CASE, self.repo / ".git/index", self.config]
        return {str(path): path.read_bytes() for path in paths}

    def assert_protected(self, original):
        self.assertEqual({name: Path(name).read_bytes() for name in original}, original)

    def seed_code_issue(self):
        directory = self.state / "runs/synthetic-code-issue"
        identity = review.repository_identity(self.repo, [RULE])
        result = {"status": "issues", "snapshot_id": "synthetic-code-snapshot",
                  "current_category": "comment_quality", "summary": "Offline existing issue.",
                  "findings": [{"category": "comment_quality", "message": "Offline existing finding."}]}
        maintenance.save(directory / "input.json", {"basis": [RULE], "target_repository": identity})
        maintenance.save(directory / "result.json", result)
        maintenance.save(self.state / "latest.json", {"run": str(directory), "snapshot_id": result["snapshot_id"],
                         "status": result["status"], "target_repository": identity})
        return directory

    def fake_maintenance(self, statuses, *, behavior=None):
        """fake_maintenance() 只替换子检查边界，让真实聚合器验证子证据及未完成优先级。"""
        observed = []
        templates = self.root / "input-templates"
        child_inputs = {}
        with patch.object(acp_model.AcpModel, "run", side_effect=ModelFailure("Synthetic input-only template.")), \
                redirect_stdout(io.StringIO()):
            for kind in ["rules", "fixtures"]:
                material_check.check_materials(self.repo, self.config, templates, kind)
                pointer = json.loads((templates / f"latest-quality-{kind}.json").read_text())
                child_inputs[kind + "-quality"] = json.loads((Path(pointer["run"]) / "input.json").read_text())
            rules_check.check_rules(self.repo, self.config, templates)
            pointer = json.loads((templates / "latest-rules-check.json").read_text())
            child_inputs["rules-regression"] = json.loads((Path(pointer["run"]) / "input.json").read_text())

        def child(state, stage):
            observed.append((stage, Path(state)))
            status = statuses[stage]
            code = {"passed": 0, "failed": 1, "incomplete": 2}[status]
            if behavior:
                replacement = behavior(stage, Path(state))
                if replacement is not None:
                    return replacement
            directory = Path(state) / "synthetic-checks" / stage
            frozen = child_inputs[stage]
            snapshot = hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            maintenance.save(directory / "input.json", frozen)
            maintenance.save(directory / "result.json", {"status": status, "snapshot_id": snapshot,
                                                          "interrupted": False})
            pointer_name = {"rules-quality": "latest-quality-rules.json",
                            "fixtures-quality": "latest-quality-fixtures.json",
                            "rules-regression": "latest-rules-check.json"}[stage]
            maintenance.save(Path(state) / pointer_name,
                             {"run": str(directory), "snapshot_id": snapshot, "status": status})
            return code

        with patch.object(maintenance, "ENGINE", self.engine), \
                patch.object(maintenance, "check_materials", side_effect=lambda repo, config, state, kind:
                             child(state, kind + "-quality")), \
                patch.object(maintenance, "check_rules", side_effect=lambda repo, config, state:
                             child(state, "rules-regression")), redirect_stdout(io.StringIO()):
            code = maintenance.maintain(self.repo, self.config, self.state)
        return code, observed

    def test_real_maintain_binds_three_child_snapshots_to_one_unchanged_suite(self):
        protected = self.protected_bytes()
        code_run = self.seed_code_issue()
        code_records = {path: path.read_bytes() for path in [self.state / "latest.json", code_run / "result.json"]}
        result = self.make("review-engine")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        directory, report = self.latest()
        self.assertEqual(report["status"], "passed")
        self.assertEqual([stage["stage"] for stage in report["stages"]], list(maintenance.STAGES))
        frozen = json.loads((directory / "input.json").read_text())
        self.assertEqual(frozen["snapshot_id"], report["snapshot_id"])
        self.assertEqual(frozen["frozen"]["documents"], {RULE: (self.repo / RULE).read_text(), CASE: FIXTURE})
        self.assertEqual(len({stage["snapshot_id"] for stage in report["stages"]}), 3)
        for stage in report["stages"]:
            child = Path(stage["run"])
            self.assertTrue(child.is_relative_to(directory / "checks"))
            child_result = json.loads((child / "result.json").read_text())
            self.assertEqual(stage["snapshot_id"], child_result["snapshot_id"])
            self.assertEqual(stage["status"], "passed")
            self.assertEqual(stage["exit_code"], 0)
        calls = self.calls()
        self.assertEqual([(call["stage"], call["packet"].get("kind")) for call in calls],
                         [("quality", "rules"), ("quality", "fixtures"),
                          ("blind", None), ("blind", None), ("audit", None)])
        for call in calls:
            self.assertNotIn("ORDINARY_SOURCE_MUST_NOT_REACH", call["prompt"])
            self.assertEqual(call["packet"]["snapshot_id"], next(
                stage["snapshot_id"] for stage in report["stages"]
                if stage["stage"] == ({"rules": "rules-quality", "fixtures": "fixtures-quality"}.get(
                    call["packet"].get("kind"), "rules-regression"))))
        self.assert_protected(protected)
        self.assertEqual({path: path.read_bytes() for path in code_records}, code_records)
        self.assertFalse((self.repo / ".git/hooks/commit-msg").exists())

    def test_material_engine_and_configuration_changes_cannot_pass_the_aggregate(self):
        for path in [self.repo / RULE, self.engine / "workflow.md", self.config]:
            with self.subTest(path=path.relative_to(self.repo)):
                original = path.read_bytes()
                self.set_mode("normal", {"path": str(path), "suffix": "\n"})
                result = self.command("maintain")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                _, report = self.latest()
                self.assertEqual(report["status"], "incomplete")
                self.assertTrue(report["input_errors"])
                self.assertIn("发生变化", " ".join(report["input_errors"]))
                path.write_bytes(original)

    def test_stable_child_version_b_cannot_join_version_a_suite_after_inputs_are_restored(self):
        """test_stable_child_version_b_cannot_join_version_a_suite_after_inputs_are_restored() 用真实子检查保留首尾相同而中间不同的反证。

        第二阶段完整读取 B 并自行通过，第三阶段前恢复 A，因此只能靠子检查冻结证据阻止混用。
        """
        wrapper = self.root / "aba-maintain.py"
        wrapper.write_text(ABA_RUNNER)
        for changed in [self.repo / CASE, self.engine / "workflow.md", self.config]:
            with self.subTest(changed=changed.relative_to(self.repo)):
                self.set_mode("normal")
                original = changed.read_bytes()
                protected = self.protected_bytes()
                result = self.execute([sys.executable, str(wrapper), str(self.repo), str(self.config),
                                       str(self.state), str(changed)], check=False)
                directory, report = self.latest()
                self.assertEqual(changed.read_bytes(), original)
                self.assert_protected(protected)
                suite = json.loads((directory / "input.json").read_text())["frozen"]
                child_inputs = {}
                for stage, pattern in [("rules-quality", "material-checks/rules/run-*"),
                                       ("fixtures-quality", "material-checks/fixtures/run-*"),
                                       ("rules-regression", "rules-checks/run-*")]:
                    child = next((directory / "checks").glob(pattern))
                    child_result = json.loads((child / "result.json").read_text())
                    self.assertEqual(child_result["status"], "passed", stage)
                    child_inputs[stage] = json.loads((child / "input.json").read_text())
                self.assertEqual(child_inputs["rules-quality"]["documents"], {RULE: suite["documents"][RULE]})
                self.assertEqual(child_inputs["rules-regression"]["documents"], suite["documents"])
                middle = child_inputs["fixtures-quality"]
                if changed == self.repo / CASE:
                    self.assertNotEqual(middle["documents"][CASE], suite["documents"][CASE])
                elif changed == self.engine / "workflow.md":
                    self.assertNotEqual(middle["engine_sha256"]["workflow.md"], suite["engine_files"]["workflow.md"])
                else:
                    self.assertNotEqual(middle["config_sha256"], suite["config_sha256"])
                self.assertEqual([call["stage"] for call in self.calls()],
                                 ["quality", "quality", "blind", "blind", "audit"])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(report["status"], "incomplete")
                self.assertEqual(report["stages"][1]["status"], "incomplete")
                self.assertEqual(report["input_errors"], [])
                self.assertIs(report["interrupted"], False)

    def test_empty_cases_and_missing_rules_are_incomplete_without_false_zero_coverage(self):
        cases = self.repo / CASE
        cases.unlink()
        self.write(".clarity/current/cases/.gitkeep", "")
        result = self.command("maintain")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        _, report = self.latest()
        self.assertEqual([stage["status"] for stage in report["stages"]],
                         ["passed", "incomplete", "incomplete"])
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(report["snapshot_id"], "unresolved")
        self.set_mode("normal")
        self.write(CASE, FIXTURE)
        (self.repo / RULE).unlink()
        result = self.command("maintain")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        _, report = self.latest()
        self.assertEqual([stage["status"] for stage in report["stages"]],
                         ["incomplete", "passed", "incomplete"])
        self.assertEqual([(call["stage"], call["packet"].get("kind")) for call in self.calls()],
                         [("quality", "fixtures")])

    def test_failed_stages_continue_and_incomplete_takes_precedence(self):
        scenarios = [(["failed", "passed", "failed"], 1), (["failed", "incomplete", "passed"], 2)]
        for statuses, expected in scenarios:
            with self.subTest(statuses=statuses):
                code, calls = self.fake_maintenance(dict(zip(maintenance.STAGES, statuses)))
                self.assertEqual(code, expected)
                _, report = self.latest()
                self.assertEqual([stage["status"] for stage in report["stages"]], statuses)
                self.assertEqual([stage for stage, state in calls], list(maintenance.STAGES))
                self.assertEqual(report["current_stage"], "rules-quality")

    def test_real_model_failure_retains_incomplete_and_unrun_fragment_states(self):
        self.set_mode("model_failure")
        result = self.command("maintain")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        _, report = self.latest()
        self.assertEqual([stage["status"] for stage in report["stages"]], ["incomplete"] * 3)
        self.assertIs(report["interrupted"], False)
        regression = json.loads((Path(report["stages"][2]["run"]) / "result.json").read_text())
        self.assertEqual([sample["status"] for sample in regression["samples"]], ["incomplete", "not_run"])
        self.assertEqual(regression["summary_counts"]["not_run"], 1)
        self.assertEqual(regression["summary_counts"]["matched"], 0)
        self.assertEqual([call["stage"] for call in self.calls()], ["quality", "quality", "blind"])

    def test_real_checker_interruption_stops_the_suite_after_the_interrupted_child_returns(self):
        """test_real_checker_interruption_stops_the_suite_after_the_interrupted_child_returns() 核对子检查保存的中断状态仍能阻止后续模型调用。

        假模型在真实质量检查器内部抛中断；普通调用失败与中断分开，不能仅凭退出码 2 判断是否停止。
        """
        wrapper = self.root / "interrupt-maintain.py"
        wrapper.write_text(INTERRUPT_RUNNER)
        trace = self.root / "interrupted-calls.json"
        for interrupt_at in [1, 2]:
            with self.subTest(interrupt_at=interrupt_at):
                self.set_mode("normal")
                result = self.execute([sys.executable, str(wrapper), str(self.repo), str(self.config),
                                       str(self.state), str(trace), str(interrupt_at)], check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                directory, report = self.latest()
                expected = ["passed"] * (interrupt_at - 1) + ["incomplete"] + ["not_run"] * (3 - interrupt_at)
                self.assertEqual([stage["status"] for stage in report["stages"]], expected)
                self.assertIs(report["interrupted"], True)
                self.assertEqual(len(json.loads(trace.read_text())), interrupt_at)
                kind = "rules" if interrupt_at == 1 else "fixtures"
                child = next((directory / "checks/material-checks" / kind).glob("run-*/result.json"))
                self.assertIs(json.loads(child.read_text())["interrupted"], True)
                self.assertEqual(len(self.calls()), interrupt_at - 1)

    def test_subcheck_exception_is_incomplete_and_does_not_hide_other_results(self):
        def failure(stage, state):
            if stage == "fixtures-quality":
                raise OSError("Synthetic child record unavailable")
        code, calls = self.fake_maintenance(dict(zip(maintenance.STAGES, ["failed", "passed", "passed"])),
                                            behavior=failure)
        self.assertEqual(code, 2)
        _, report = self.latest()
        self.assertEqual([stage["status"] for stage in report["stages"]], ["failed", "incomplete", "passed"])
        self.assertIn("Synthetic child", report["stages"][1]["error"])
        self.assertEqual(len(calls), 3)

    def test_zero_without_a_child_result_is_incomplete(self):
        code, _ = self.fake_maintenance(dict.fromkeys(maintenance.STAGES, "passed"),
                                       behavior=lambda stage, state: 0 if stage == "rules-quality" else None)
        self.assertEqual(code, 2)
        _, report = self.latest()
        self.assertEqual([stage["status"] for stage in report["stages"]], ["incomplete", "passed", "passed"])
        self.assertNotIn("run", report["stages"][0])

    def test_interruption_preserves_not_run_stages(self):
        def interrupt(stage, state):
            raise KeyboardInterrupt()
        code, calls = self.fake_maintenance(dict.fromkeys(maintenance.STAGES, "passed"), behavior=interrupt)
        self.assertEqual(code, 2)
        _, report = self.latest()
        self.assertEqual([stage["status"] for stage in report["stages"]], ["incomplete", "not_run", "not_run"])
        self.assertEqual(len(calls), 1)

    def test_subcheck_latest_cannot_import_a_record_from_another_suite(self):
        foreign = self.state / "material-checks/rules/run-foreign"
        maintenance.save(foreign / "result.json", {"status": "passed", "snapshot_id": "foreign"})
        maintenance.save(self.state / "latest-quality-rules.json",
                         {"run": str(foreign), "status": "passed", "snapshot_id": "foreign"})

        def contaminate(stage, state):
            if stage == "rules-quality":
                maintenance.save(state / "latest-quality-rules.json",
                                 {"run": str(foreign), "status": "passed", "snapshot_id": "foreign"})
                return 0
        code, calls = self.fake_maintenance(dict.fromkeys(maintenance.STAGES, "passed"), behavior=contaminate)
        self.assertEqual(code, 2)
        directory, report = self.latest()
        self.assertEqual(report["stages"][0]["status"], "incomplete")
        self.assertIn("不属于本次维护", report["stages"][0]["error"])
        self.assertTrue(all(state == directory / "checks" for stage, state in calls))

    def test_stale_global_latest_is_not_a_substitute_for_a_missing_child_pointer(self):
        foreign = self.state / "rules-checks/run-previous"
        maintenance.save(foreign / "result.json", {"status": "passed", "snapshot_id": "previous"})
        maintenance.save(self.state / "latest-rules-check.json",
                         {"run": str(foreign), "status": "passed", "snapshot_id": "previous"})
        code, _ = self.fake_maintenance(dict.fromkeys(maintenance.STAGES, "passed"),
                                       behavior=lambda stage, state: 0 if stage == "rules-regression" else None)
        self.assertEqual(code, 2)
        _, report = self.latest()
        self.assertEqual(report["stages"][2]["status"], "incomplete")
        self.assertNotIn("run", report["stages"][2])

    def test_child_exit_status_snapshot_and_result_must_agree(self):
        for changed in ["exit", "snapshot", "status"]:
            with self.subTest(changed=changed):
                def inconsistent(stage, state):
                    if stage != "rules-quality":
                        return None
                    directory = state / "synthetic-checks/inconsistent"
                    maintenance.save(directory / "result.json", {"status": "passed", "snapshot_id": "same"})
                    maintenance.save(state / "latest-quality-rules.json",
                                     {"run": str(directory), "status": "failed" if changed == "status" else "passed",
                                      "snapshot_id": "different" if changed == "snapshot" else "same"})
                    return 1 if changed == "exit" else 0
                code, calls = self.fake_maintenance(dict.fromkeys(maintenance.STAGES, "passed"), behavior=inconsistent)
                self.assertEqual(code, 2)
                _, report = self.latest()
                self.assertEqual(report["stages"][0]["status"], "incomplete")
                self.assertEqual(len(calls), 3)

    def test_make_routes_all_five_kinds_of_work_to_the_existing_cli(self):
        binary = self.root / "bin"
        binary.mkdir()
        capture = self.root / "make-route.json"
        fake_python = binary / "python3"
        fake_python.write_text("#!" + sys.executable + "\nimport json, os, sys\nfrom pathlib import Path\n"
                               + "Path(" + repr(str(capture)) + ").write_text(json.dumps(sys.argv[1:]))\n")
        fake_python.chmod(0o755)
        environment = {**self.environment, "PATH": str(binary) + os.pathsep + self.environment["PATH"]}
        routes = {"review": ["review", "--target", "HEAD"],
                  "review-staged": ["review", "--target", "index"],
                  "review-rules-quality": ["check-rules-quality"],
                  "review-fixtures-quality": ["check-fixtures-quality"],
                  "review-rules": ["check-rules"], "review-engine": ["maintain"]}
        for target, arguments in routes.items():
            with self.subTest(target=target):
                result = self.make(target, environment=environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(capture.read_text()), [".clarity/engine/runner/review.py", *arguments])

    def test_material_make_targets_execute_the_real_offline_checks(self):
        protected = self.protected_bytes()
        self.seed_code_issue()
        latest = (self.state / "latest.json").read_bytes()
        routes = [("review-rules-quality", "latest-quality-rules.json", [("quality", "rules")]),
                  ("review-fixtures-quality", "latest-quality-fixtures.json", [("quality", "fixtures")]),
                  ("review-rules", "latest-rules-check.json", [("blind", None), ("blind", None), ("audit", None)])]
        for target, pointer, expected in routes:
            with self.subTest(target=target):
                self.set_mode("normal")
                result = self.make(target)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads((self.state / pointer).read_text())["status"], "passed")
                self.assertEqual([(call["stage"], call["packet"].get("kind")) for call in self.calls()], expected)
        self.assertEqual((self.state / "latest.json").read_bytes(), latest)
        self.assert_protected(protected)

    def literal_response(self):
        marker = self.root / "MAKE_OR_SHELL_MUST_NOT_CREATE_THIS"
        text = ("Literal response: $(shell touch " + str(marker) + ")\n"
                + "`touch " + str(marker) + "` $(touch " + str(marker) + ")\n"
                + "price $5; $$HOME; ${HOME}; quotes 'single' \"double\"; slash \\; final\n")
        return marker, text

    def test_make_code_response_preserves_dollars_quotes_commands_and_newlines(self):
        directory = self.seed_code_issue()
        protected = self.protected_bytes()
        unchanged = {path: path.read_bytes() for path in
                     [self.state / "latest.json", directory / "input.json", directory / "result.json"]}
        marker, text = self.literal_response()
        result = self.make("review-respond", text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        responses = json.loads((self.state / "responses.json").read_text())
        self.assertEqual(responses[-1]["human_text"], text)
        self.assertEqual(responses[-1]["snapshot_id"], "synthetic-code-snapshot")
        self.assertEqual(responses[-1]["category"], "comment_quality")
        self.assertEqual(responses[-1]["target_repository"], review.repository_identity(self.repo, [RULE]))
        self.assertEqual({path: path.read_bytes() for path in unchanged}, unchanged)
        self.assert_protected(protected)

    def test_maintenance_responses_bind_target_suite_and_selected_stage_without_changing_results(self):
        self.seed_code_issue()
        self.set_mode("quality_failed")
        result = self.command("maintain")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        directory, report = self.latest()
        protected = self.protected_bytes()
        paths = [self.state / "latest.json", self.state / "latest-maintenance.json",
                 directory / "input.json", directory / "result.json", directory / "report.md"]
        paths.extend(Path(stage["run"]) / "result.json" for stage in report["stages"])
        unchanged = {path: path.read_bytes() for path in paths}
        marker, text = self.literal_response()
        result = self.make("review-engine-respond", text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        result = self.command("maintain-respond", "--stage", "fixtures-quality", "--text", "fixture-stage response")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        responses = json.loads((directory / "responses.json").read_text())
        self.assertEqual([response["stage"] for response in responses], ["rules-quality", "fixtures-quality"])
        self.assertEqual(responses[0]["human_text"], text)
        for response, stage in zip(responses, report["stages"]):
            self.assertEqual(response["target_repository"], maintenance.target_identity(self.repo))
            self.assertEqual(response["run"], str(directory))
            self.assertEqual(response["snapshot_id"], report["snapshot_id"])
            self.assertEqual(response["stage_record"], stage)
            child_path = Path(stage["run"]) / "result.json"
            self.assertEqual(response["stage_result_ref"],
                             {"path": str(child_path), "snapshot_id": stage["snapshot_id"],
                              "status": stage["status"], "sha256": hashlib.sha256(child_path.read_bytes()).hexdigest()})
            self.assertNotIn("stage_result", response)
        self.assertFalse((self.state / "responses.json").exists())
        self.assertEqual({path: path.read_bytes() for path in unchanged}, unchanged)
        self.assert_protected(protected)

    def test_invalid_maintenance_responses_preserve_latest_and_existing_response_records(self):
        self.seed_code_issue()
        self.fake_maintenance(dict(zip(maintenance.STAGES, ["failed", "passed", "passed"])))
        directory, report = self.latest()
        response_path = directory / "responses.json"
        maintenance.save(response_path, [{"human_text": "Existing response must survive."}])
        pointer_path = self.state / "latest-maintenance.json"
        pointer = json.loads(pointer_path.read_text())
        code_latest = (self.state / "latest.json").read_bytes()
        original_responses = response_path.read_bytes()
        scenarios = [("empty", {}, ["--text", " \n "]),
                     ("passed-stage", {}, ["--stage", "fixtures-quality", "--text", "no issue here"]),
                     ("other-target", {"target_repository": {"worktree_root": "/another/worktree"}}, ["--text", "wrong target"]),
                     ("wrong-snapshot", {"snapshot_id": "another-snapshot"}, ["--text", "wrong snapshot"]),
                     ("outside-suite", {"run": str(self.root / "foreign-suite")}, ["--text", "wrong suite"])]
        for name, changes, arguments in scenarios:
            with self.subTest(name=name):
                maintenance.save(pointer_path, {**pointer, **changes})
                latest_before = pointer_path.read_bytes()
                result = self.command("maintain-respond", *arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(pointer_path.read_bytes(), latest_before)
                self.assertEqual((self.state / "latest.json").read_bytes(), code_latest)
                self.assertEqual(response_path.read_bytes(), original_responses)
                self.assertEqual(json.loads((directory / "result.json").read_text()), report)

    def test_invalid_code_responses_preserve_latest_instead_of_creating_an_incomplete_review(self):
        directory = self.seed_code_issue()
        response_path = self.state / "responses.json"
        maintenance.save(response_path, [{"human_text": "Existing code response."}])
        original_responses = response_path.read_bytes()
        pointer_path = self.state / "latest.json"
        pointer = json.loads(pointer_path.read_text())
        original_runs = set((self.state / "runs").iterdir())
        scenarios = [({}, ["--text", " \n "]),
                     ({"snapshot_id": "wrong-snapshot"}, ["--text", "wrong snapshot"]),
                     ({"target_repository": {"worktree_root": "/another/worktree"}}, ["--text", "wrong target"]),
                     ({"run": str(self.root / "foreign-code-review")}, ["--text", "wrong review"])]
        for changes, arguments in scenarios:
            with self.subTest(changes=changes, arguments=arguments):
                maintenance.save(pointer_path, {**pointer, **changes})
                latest_before = pointer_path.read_bytes()
                result = self.command("respond", *arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(pointer_path.read_bytes(), latest_before)
                self.assertEqual(response_path.read_bytes(), original_responses)
                self.assertEqual(set((self.state / "runs").iterdir()), original_runs)
                self.assertEqual(json.loads((directory / "result.json").read_text())["status"], "issues")


if __name__ == "__main__":
    unittest.main()
