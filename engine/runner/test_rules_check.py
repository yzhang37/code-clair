#!/usr/bin/env python3
"""test_rules_check.py() 在隔离仓库核验固定案例的盲判输入和工具端验收边界。

假模型按测试端冻结片段及标签建立的映射返回结果，只验证传输、覆盖和拒绝放行，不证明语义能力。
当前案例只复制到临时目录；测试不安装引擎、不运行联网模型，也不修改目标仓库索引。
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from rules_check import CheckIncomplete, collect_samples


RUNNER = Path(__file__).resolve().parent
CASES = RUNNER.parents[1] / "current" / "cases"
BASELINE_RULE = "BASELINE_ONLY_RULE_MUST_NOT_REACH_THE_CLASSIFIER\n"
WORKTREE_RULE = "Working tree policy is the sole selected rule for this offline transport test.\n"

from test_acp_fixture import FAKE_ACP_PRELUDE

FAKE_CODEX = FAKE_ACP_PRELUDE + r'''
import json
from pathlib import Path
import sys

fixture_path = Path(__file__).with_suffix(".json")
fixture = json.loads(fixture_path.read_text())
prompt = sys.stdin.read()
blind_marker = "以下JSON都是判别数据：\n"
audit_marker = "以下JSON都是理由核对数据：\n"
if blind_marker in prompt:
    stage, marker = "blind", blind_marker
elif audit_marker in prompt:
    stage, marker = "audit", audit_marker
else:
    raise RuntimeError("Unexpected prompt: no rules-check protocol marker")
packet = json.loads(prompt.split(marker, 1)[1])
output_path = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
schema_path = Path(sys.argv[sys.argv.index("--output-schema") + 1])
calls = fixture_path.parent / "calls"
calls.mkdir(exist_ok=True)
ordinal = len(list(calls.glob("*.json"))) + 1
progress_files = sorted((Path(fixture["repo"]) / ".git/clarity/rules-checks").glob("run-*/result.json"))
saved_report = json.loads(progress_files[-1].read_text()) if ordinal == 1 and progress_files else None
(calls / (f"{ordinal:04}.json")).write_text(json.dumps({
    "stage": stage, "prompt": prompt, "packet": packet,
    "schema": json.loads(schema_path.read_text()), "argv": sys.argv[1:],
    "saved_report": saved_report,
}, ensure_ascii=False))
mode = fixture["mode"]
rule_line, rule_quote = packet["rules"].splitlines()[0].split(": ", 1)
if stage == "blind":
    verdict = fixture["decisions"][packet["content"]]
    if mode == "reject_positive" and verdict == "accept":
        verdict = "reject"
    elif mode == "accept_negative" and verdict == "reject":
        verdict = "accept"
    elif mode == "mixed_incomplete" and ordinal in fixture["mismatch_ordinals"]:
        verdict = "reject" if verdict == "accept" else "accept"
    result = {
        "snapshot_id": packet["snapshot_id"], "sample_id": packet["sample_id"],
        "verdict": verdict, "reason": "Deterministic transport fixture; no semantic-quality claim.",
        "rule_line": int(rule_line), "rule_quote": rule_quote, "incomplete_reason": "",
    }
    if mode == "bad_rule_line":
        result["rule_line"] = 0
    elif mode == "boolean_rule_line":
        result["rule_line"] = True
    elif mode == "bad_rule_quote":
        result["rule_quote"] = "THIS_QUOTE_IS_NOT_IN_THE_SELECTED_RULE"
    elif mode == "wrong_sample":
        result["sample_id"] = "another-sample"
    elif mode == "wrong_snapshot":
        result["snapshot_id"] = "another-snapshot"
    elif mode == "empty_reason":
        result["reason"] = " "
    elif mode == "incomplete_verdict" or (mode == "mixed_incomplete" and ordinal == fixture["sample_count"]):
        result["verdict"] = "incomplete"
        result["incomplete_reason"] = "Synthetic missing evidence."
    elif mode == "contradictory_incomplete":
        result["incomplete_reason"] = "Synthetic missing evidence."
    elif mode == "missing_field":
        del result["reason"]
    elif mode == "model_failure":
        sys.exit(9)
    elif mode in {"change_rules", "change_case", "change_line_endings"} and ordinal == 1:
        target = Path(fixture["repo"]) / fixture["change_target"]
        if mode == "change_line_endings":
            target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
        else:
            target.write_text(target.read_text() + "\nMATERIAL_CHANGED_DURING_CLASSIFICATION\n")
else:
    case_line, case_quote = next(line.split(": ", 1) for line in packet["fixture"].splitlines()
                                 if line.split(": ", 1)[1].strip())
    checks = [{"sample_id": item["sample_id"], "aligned": True,
               "case_line": int(case_line), "case_quote": case_quote,
               "reason": "Deterministic reason-audit transport fixture."}
              for item in packet["predictions"]]
    result = {"snapshot_id": packet["snapshot_id"], "case_id": packet["case_id"],
              "checks": checks, "summary": "Offline fixture; no semantic-quality claim.",
              "incomplete_reason": ""}
    if mode == "audit_mismatch":
        result["checks"][0]["aligned"] = False
    elif mode == "audit_missing_check":
        result["checks"] = checks[:-1]
    elif mode == "audit_duplicate_check":
        result["checks"] = [*checks, checks[0]]
    elif mode == "audit_bad_quote":
        result["checks"][0]["case_quote"] = "THIS_QUOTE_IS_NOT_IN_THE_CASE"
    elif mode == "audit_wrong_case":
        result["case_id"] = "another-case"
    elif mode == "audit_incomplete":
        result["incomplete_reason"] = "Synthetic missing case evidence."

output_path.write_text("{invalid JSON" if mode == "invalid_json"
                       else json.dumps(result, ensure_ascii=False))
events = [{"type": "thread.started", "thread_id": "offline-rules-check"},
          {"type": "turn.started"}]
if mode == "tool_event":
    events.append({"type": "item.completed", "item": {"id": "forbidden", "type": "command_execution"}})
events.extend([
    {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "fixture"}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
])
for event in events:
    print(json.dumps(event))
'''


class IsolatedRulesCheckTests(unittest.TestCase):
    """IsolatedRulesCheckTests() 只将临时资料和固定假模型交给新增规则验收命令。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-rules-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("GIT_") and key not in {"CODEX_API_KEY", "OPENAI_API_KEY"}}
        self.environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                 "GIT_TERMINAL_PROMPT": "0", "CODEX_HOME": str(self.root / "empty-home"),
                                 "OPENAI_API_KEY": "synthetic-offline-fixture-key"})
        self.fake = self.root / "fake-rules-codex.py"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_CODEX)
        self.fake.chmod(0o755)
        self.fixture_path = self.fake.with_suffix(".json")
        self.config_path = self.root / "rules-check-config.json"
        self.config_path.write_text(json.dumps({"agent": "custom", "agent_command": [str(self.fake)],
                                             "codex_bin": str(self.fake), "model": "offline-rules-check",
                                                "timeout": 3}))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Offline Rules Fixture")
        self.git("config", "user.email", "offline-rules@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write(".clarity/current/rules.md", BASELINE_RULE)
        for path in CASES.rglob("*.md"):
            self.write(".clarity/current/cases/" + path.relative_to(CASES).as_posix(), path.read_text())
        self.write(".clarity/current/sources.md", "SOURCES_SECRET_MUST_NOT_REACH_THE_CLASSIFIER\n")
        self.write("ordinary-source.py", "ORDINARY_SOURCE_MUST_NOT_REACH_THE_CLASSIFIER = True\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixed case baseline")
        self.write(".clarity/current/rules.md", WORKTREE_RULE)
        self.fixture_documents = {".clarity/current/rules.md": WORKTREE_RULE}
        self.fixture_documents.update({path.relative_to(self.repo).as_posix(): path.read_text()
                                       for path in (self.repo / ".clarity/current/cases").rglob("*.md")})
        self.sample_plan = collect_samples(self.fixture_documents)
        self.sample_count = len(self.sample_plan)
        self.case_groups = {}
        for sample in self.sample_plan:
            self.case_groups.setdefault(sample["case_path"], []).append(sample)
        self.case_count = len(self.case_groups)
        self.set_mode("normal")

    def execute(self, command, *, check=True, cwd=None):
        result = subprocess.run(command, cwd=cwd or self.repo, env=self.environment, text=True,
                                capture_output=True, timeout=30)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, *arguments, **kwargs):
        return self.execute(["git", "-C", str(self.repo), *arguments], **kwargs)

    def write(self, relative, content):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    @property
    def state(self):
        return self.repo / ".git" / "clarity"

    def set_mode(self, mode, *, samples=None):
        for path in (self.root / "calls").glob("*.json"):
            path.unlink()
        selected = self.sample_plan if samples is None else samples
        decisions = {}
        for sample in selected:
            if sample["content"] in decisions:
                self.assertEqual(decisions[sample["content"]], sample["expected"],
                                 "相同片段需要相同的离线传输答案")
            decisions[sample["content"]] = sample["expected"]
        fixture = {"mode": mode, "decisions": decisions, "repo": str(self.repo),
                   "sample_count": len(selected)}
        if mode == "mixed_incomplete":
            self.assertGreaterEqual(len(selected), 4)
            fixture["mismatch_ordinals"] = [1, max(2, len(selected) // 2), len(selected) - 1]
        if mode in {"change_rules", "change_case", "change_line_endings"}:
            fixture["change_target"] = (".clarity/current/rules.md" if mode == "change_rules"
                                        else next(iter(self.case_groups)))
        self.fixture_path.write_text(json.dumps(fixture, ensure_ascii=False))

    def expected_summary(self, *, matched=0, mismatch=0, incomplete=0, not_run=0, running=0):
        return {"case_count": self.case_count, "subcase_count": self.sample_count,
                "matched": matched, "mismatch": mismatch, "incomplete": incomplete,
                "not_run": not_run, "running": running}

    def check_rules(self):
        return self.execute([sys.executable, str(RUNNER / "review.py"), "check-rules",
                             "--repo", str(self.repo), "--config", str(self.config_path)], check=False)

    def latest_result(self):
        latest = json.loads((self.state / "latest-rules-check.json").read_text())
        return json.loads((Path(latest["run"]) / "result.json").read_text())

    def latest_markdown(self):
        latest = json.loads((self.state / "latest-rules-check.json").read_text())
        return (Path(latest["run"]) / "report.md").read_text()

    def calls(self):
        return [json.loads(path.read_text()) for path in sorted((self.root / "calls").glob("*.json"))]

    def material_bytes(self):
        return {str(path.relative_to(self.repo)): path.read_bytes()
                for path in (self.repo / ".clarity" / "current").rglob("*") if path.is_file()}

    def test_all_frozen_fragments_are_blindly_classified_then_independently_audited(self):
        material = self.material_bytes()
        index = (self.repo / ".git" / "index").read_bytes()
        head = self.git("rev-parse", "HEAD").stdout
        self.state.mkdir(parents=True)
        previous_daily = b'{"run":"untouched-daily-review"}\n'
        (self.state / "latest.json").write_bytes(previous_daily)
        result = self.check_rules()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = self.latest_result()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["samples"]), self.sample_count)
        self.assertEqual(len({item["case_path"] for item in report["samples"]}), self.case_count)
        for expected in ("accept", "reject"):
            self.assertEqual(sum(item["expected"] == expected for item in report["samples"]),
                             sum(item["expected"] == expected for item in self.sample_plan))
        self.assertEqual([{key: item[key] for key in planned}
                          for item, planned in zip(report["samples"], self.sample_plan)], self.sample_plan)
        self.assertTrue(all(item["actual"] == item["expected"] for item in report["samples"]))
        calls = self.calls()
        self.assertEqual([call["stage"] for call in calls],
                         ["blind"] * self.sample_count + ["audit"] * self.case_count)
        first_progress = calls[0]["saved_report"]
        self.assertEqual(len(first_progress["samples"]), self.sample_count)
        self.assertEqual(first_progress["samples"][0]["status"], "running")
        self.assertTrue(all(item["actual"] is None and item["status"] == "not_run"
                            for item in first_progress["samples"][1:]))
        self.assertEqual(first_progress["summary_counts"]["running"], 1)
        self.assertEqual(first_progress["summary_counts"]["not_run"], self.sample_count - 1)
        blind = calls[:self.sample_count]
        self.assertEqual(len({call["packet"]["sample_id"] for call in blind}), self.sample_count)
        for call in blind:
            packet = call["packet"]
            self.assertEqual(set(packet), {"snapshot_id", "sample_id", "rules", "context", "content"})
            self.assertEqual(packet["rules"], "1: " + WORKTREE_RULE.rstrip())
            self.assertEqual(packet["snapshot_id"], report["snapshot_id"])
            self.assertIsInstance(packet["context"], list)
            self.assertTrue(all(isinstance(text, str) for text in packet["context"]))
            self.assertNotIn("BASELINE_ONLY_RULE", call["prompt"])
            self.assertNotIn("SOURCES_SECRET", call["prompt"])
            self.assertNotIn("ORDINARY_SOURCE", call["prompt"])
            for forbidden in ("## 正例", "## 反例", "所有者原文", "来源：", "0001.",
                              "任务数量正确不等于语言组织正确", "复制标记会让按标记检索", "这是需要保留的现有事实"):
                self.assertNotIn(forbidden, call["prompt"])
        quoted = [call["packet"]["content"] for call in blind if "肯定会打印" in call["packet"]["content"]]
        self.assertEqual(len(quoted), 1, "审查判断的 blockquote 也必须接受独立盲判")
        samples_by_id = {sample["sample_id"]: sample for sample in report["samples"]}
        for call in calls[self.sample_count:]:
            packet = call["packet"]
            self.assertIn("判定标准仅为 rules", call["prompt"])
            self.assertEqual(set(packet), {"snapshot_id", "case_id", "rules", "fixture", "predictions"})
            self.assertTrue(packet["predictions"])
            self.assertTrue(all(set(item) == {"sample_id", "verdict", "reason", "rule_line", "rule_quote",
                                               "case_line", "content"}
                                for item in packet["predictions"]))
            selected_paths = set()
            for prediction in packet["predictions"]:
                frozen_sample = samples_by_id[prediction["sample_id"]]
                selected_paths.add(frozen_sample["case_path"])
                self.assertEqual(prediction["case_line"], frozen_sample["case_line"])
                self.assertEqual(prediction["content"], frozen_sample["content"])
                original_lines = material[frozen_sample["case_path"]].decode().splitlines()
                content_lines = prediction["content"].splitlines()
                first = prediction["case_line"] - 1
                original_fragment = original_lines[first:first + len(content_lines)]
                if all(re.match(r"^\s*>\s?", line) for line in original_fragment):
                    original_fragment = [re.sub(r"^\s*>\s?", "", line) for line in original_fragment]
                self.assertEqual(original_fragment, content_lines)
            self.assertEqual(len(selected_paths), 1)
        self.assertEqual(self.material_bytes(), material)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)
        self.assertEqual((self.state / "latest.json").read_bytes(), previous_daily)
        self.assertFalse((self.state / "engine").exists())

    def test_case_labels_explanations_and_original_answers_do_not_leak_into_blind_prompts(self):
        for path in (self.repo / ".clarity" / "current" / "cases").glob("*.md"):
            path.unlink()
        document = """# CASE_TITLE_SECRET

来源：CASE_SOURCE_SECRET

## 目的

检查限定片段中的操作与原因说明 SCOPE_CONTEXT_MARKER。

## 背景

适用条件是 FACT_CONTEXT_MARKER 时，输入已固定。
WHY_CONTEXT_SECRET 不属于事实前提。

## 正例：POSITIVE_LABEL_SECRET

```text
ERROR Posting result persistence failed
```

POSITIVE_EXPLANATION_SECRET

## 反例：NEGATIVE_LABEL_SECRET

```text
ERROR Posting failed
```

NEGATIVE_EXPLANATION_SECRET

## 判断理由

REASON_SECRET

## 边界

BOUNDARY_SECRET

## 所有者原文（完整保留）

ORIGINAL_SECRET

### 正例：ORIGINAL_LABEL_SECRET

```text
ORIGINAL_CONTENT_SECRET
```

### 反例：ORIGINAL_NEGATIVE_LABEL_SECRET

> ORIGINAL_NEGATIVE_CONTENT_SECRET
"""
        self.write(".clarity/current/cases/opaque-fixture.md", document)
        self.set_mode("normal", samples=collect_samples({".clarity/current/rules.md": WORKTREE_RULE,
                                                         ".clarity/current/cases/opaque-fixture.md": document}))
        result = self.check_rules()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        blind = [call for call in self.calls() if call["stage"] == "blind"]
        self.assertEqual(len(blind), 2)
        for call in blind:
            self.assertNotIn("SECRET", call["prompt"])
            self.assertNotIn("opaque-fixture", call["prompt"])
            self.assertEqual(call["packet"]["context"],
                             ["检查目标：检查限定片段中的操作与原因说明 SCOPE_CONTEXT_MARKER。",
                              "适用条件是 FACT_CONTEXT_MARKER 时，输入已固定。"])
            other = ("ERROR Posting failed" if "persistence" in call["packet"]["content"]
                     else "ERROR Posting result persistence failed")
            self.assertNotIn(other, call["prompt"])
        audit = next(call for call in self.calls() if call["stage"] == "audit")
        self.assertIn("ORIGINAL_SECRET", audit["packet"]["fixture"])

    def test_misclassified_positive_and_negative_fragments_fail_before_reason_audit(self):
        for mode in ("reject_positive", "accept_negative"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                result = self.check_rules()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                report = self.latest_result()
                self.assertEqual(report["status"], "failed")
                self.assertEqual(len(report["samples"]), self.sample_count)
                self.assertTrue(any(item["actual"] != item["expected"] for item in report["samples"]))
                self.assertFalse(any(call["stage"] == "audit" for call in self.calls()))

    def test_extraction_failure_keeps_frozen_input_and_source_location_without_model_calls(self):
        for source in (self.repo / ".clarity/current/cases").glob("*.md"):
            source.unlink()
        path = ".clarity/current/cases/invalid.md"
        documents = [
            ("# 固定案例\n\n## 目的\n\n检查当前状态，预期答案是通过。\n\n"
             "## 正例\n\n```text\nGOOD\n```\n\n## 反例\n\n> BAD\n", 5),
            ("# 固定案例\n\n## 正例\n\n```text\nGOOD\n", 5),
            ("# 固定案例\n\n## 正例\n\n```text\nGOOD\n```\n\n## 反例\n", 9),
        ]
        for document, line in documents:
            with self.subTest(line=line, document=document):
                self.write(path, document)
                result = self.check_rules()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                pointer = json.loads((self.state / "latest-rules-check.json").read_text())
                directory = Path(pointer["run"])
                frozen = json.loads((directory / "input.json").read_text())
                report = self.latest_result()
                self.assertEqual(report["status"], "incomplete")
                self.assertNotEqual(report["snapshot_id"], "unresolved")
                self.assertEqual(frozen["documents"][path], document)
                self.assertIn(f"{path}:{line}:", report["summary"])
                self.assertIn(f"{path}:{line}:", self.latest_markdown())
                scope = "范围：片段收集未完成，尚未建立完整计划"
                self.assertIn(scope, result.stdout)
                self.assertIn(scope, self.latest_markdown())
                self.assertEqual(self.calls(), [])

    def test_incomplete_classification_evidence_never_passes_or_reaches_reason_audit(self):
        modes = ("bad_rule_line", "boolean_rule_line", "bad_rule_quote", "wrong_sample", "wrong_snapshot",
                 "empty_reason", "incomplete_verdict", "contradictory_incomplete", "missing_field",
                 "model_failure", "invalid_json", "tool_event")
        for mode in modes:
            with self.subTest(mode=mode):
                self.set_mode(mode)
                result = self.check_rules()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.latest_result()["status"], "incomplete")
                self.assertFalse(any(call["stage"] == "audit" for call in self.calls()))

    def test_a_last_incomplete_classification_preserves_three_mismatches_and_the_full_plan(self):
        self.set_mode("mixed_incomplete")
        result = self.check_rules()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = self.latest_result()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(len(report["samples"]), self.sample_count)
        completed = [item for item in report["samples"] if item["actual"] in {"accept", "reject"}]
        self.assertEqual(sum(item["actual"] == item["expected"] for item in completed), self.sample_count - 4)
        self.assertEqual(sum(item["actual"] != item["expected"] for item in completed), 3)
        self.assertIsNone(report["samples"][-1]["actual"])
        self.assertEqual(report["samples"][-1]["status"], "incomplete")
        frozen_output = json.dumps(report["samples"][-1], ensure_ascii=False)
        self.assertIn('"verdict": "incomplete"', frozen_output)
        self.assertIn('"incomplete_reason": "Synthetic missing evidence."', frozen_output)
        self.assertEqual(len(self.calls()), self.sample_count)
        self.assertFalse(any(call["stage"] == "audit" for call in self.calls()))

    def test_a_first_incomplete_classification_preserves_all_cases_and_unrun_samples(self):
        self.set_mode("incomplete_verdict")
        result = self.check_rules()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = self.latest_result()
        self.assertEqual(len(report["samples"]), self.sample_count)
        self.assertEqual(len({item["case_path"] for item in report["samples"]}), self.case_count)
        self.assertIsNone(report["samples"][0]["actual"])
        self.assertEqual(report["samples"][0]["status"], "incomplete")
        self.assertTrue(all(item["actual"] is None and item["status"] == "not_run"
                            for item in report["samples"][1:]))
        self.assertEqual(report["summary_counts"],
                         self.expected_summary(incomplete=1, not_run=self.sample_count - 1))
        self.assertEqual(len(report["reason_checks"]), self.case_count)
        self.assertTrue(all(item["status"] == "not_run" for item in report["reason_checks"]))
        markdown = self.latest_markdown()
        for group in self.case_groups.values():
            self.assertIn(f"case {group[0]['case_number']}：{group[0]['case_title']}", markdown)
        self.assertEqual(len(self.calls()), 1)

    def test_successful_terminal_output_groups_frozen_subcases_under_original_cases(self):
        result = self.check_rules()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = self.latest_result()
        self.assertEqual(report["summary_counts"], self.expected_summary(matched=self.sample_count))
        self.assertIn(f"规则验收：{self.case_count} cases / {self.sample_count} subcases", result.stdout)
        markdown = self.latest_markdown()
        for case_path, planned in self.case_groups.items():
            case_number, case_title = planned[0]["case_number"], planned[0]["case_title"]
            group = [item for item in report["samples"] if item["case_path"] == case_path]
            self.assertTrue(group)
            self.assertEqual([item["subcase_number"] for item in group], list(range(1, len(planned) + 1)))
            self.assertTrue(all(item["case_number"] == case_number and item["case_title"] == case_title
                                and item["status"] == "matched" for item in group))
            self.assertIn(f"case {case_number}：{case_title}", result.stdout)
            self.assertIn(f"case {case_number}：{case_title}", markdown)
            for sample in group:
                self.assertIn(f"  - subcase {sample['subcase_number']}：{sample['subcase_title']}", result.stdout)
                self.assertIn(sample["subcase_title"], markdown)
                self.assertIsInstance(sample["elapsed_seconds"], (int, float))
        self.assertEqual(len(report["reason_checks"]), self.case_count)
        self.assertTrue(all(item["status"] == "matched" for item in report["reason_checks"]))
        latest = json.loads((self.state / "latest-rules-check.json").read_text())
        self.assertIn(str(Path(latest["run"]) / "report.md"), result.stdout)
        self.assertIn(str(Path(latest["run"]) / "result.json"), result.stdout)

    def test_reason_failures_and_incomplete_audits_are_separate_from_label_matches(self):
        for mode, status, reason_status in (("audit_mismatch", "failed", "mismatch"),
                                            ("audit_incomplete", "incomplete", "incomplete")):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                result = self.check_rules()
                self.assertEqual(result.returncode, 1 if status == "failed" else 2,
                                 result.stdout + result.stderr)
                report = self.latest_result()
                self.assertEqual(report["status"], status)
                self.assertEqual(report["summary_counts"]["matched"], self.sample_count)
                self.assertEqual(report["summary_counts"]["mismatch"], 0)
                self.assertEqual(report["summary_counts"]["incomplete"], 0)
                self.assertEqual(len(report["reason_checks"]), self.case_count)
                self.assertEqual(report["reason_checks"][0]["status"], reason_status)
                if mode == "audit_incomplete":
                    self.assertTrue(all(item["status"] == "not_run" for item in report["reason_checks"][1:]))
                else:
                    for audit in report["audits"]:
                        self.assertEqual(set(audit), {"snapshot_id", "case_id", "checks", "summary",
                                                      "incomplete_reason"})
                self.assertIn("理由核对", result.stdout)
                self.assertIn("未完成" if status == "incomplete" else "不匹配", result.stdout)
                self.assertNotIn("规则验收通过", result.stdout)
                self.assertNotIn("全部预期与理由边界符合", result.stdout)

    def test_a_correctly_rejected_negative_subcase_is_displayed_as_a_match(self):
        result = self.check_rules()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = self.latest_result()
        negative = next(item for item in report["samples"] if item["expected"] == "reject")
        self.assertEqual(negative["actual"], "reject")
        self.assertEqual(negative["status"], "matched")
        label = f"  - subcase {negative['subcase_number']}：{negative['subcase_title']}"
        lines = result.stdout.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith(label))
        visible = next(line for line in lines[start + 1:] if "实际" in line)
        self.assertIn("匹配", visible)
        self.assertNotIn("不匹配", visible)
        self.assertRegex(visible, r"预期\s*(?:reject|拒绝)")
        self.assertRegex(visible, r"实际\s*(?:reject|拒绝)")

    def test_plain_terminal_and_failure_document_retain_the_inputs_and_model_evidence(self):
        self.set_mode("mixed_incomplete")
        result = self.check_rules()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = self.latest_result()
        self.assertEqual(report["summary_counts"],
                         self.expected_summary(matched=self.sample_count - 4, mismatch=3, incomplete=1))
        self.assertNotIn("\x1b", result.stdout + result.stderr)
        self.assertNotIn("\r", result.stdout + result.stderr)
        self.assertRegex(result.stdout,
                         rf"匹配\s*{self.sample_count - 4}.*不匹配\s*3.*未完成\s*1.*未运行\s*0")
        markdown = self.latest_markdown()
        for label in ("待判原文", "事实前提", "引用规则", "模型原始理由", "未完成原因"):
            self.assertIn(label, markdown)
        for sample in report["samples"]:
            if sample["status"] not in {"mismatch", "incomplete"}:
                continue
            self.assertIn(sample["content"], markdown)
            for context in sample["context"]:
                self.assertIn(context, markdown)
            self.assertIn(sample["case_path"], markdown)
        self.assertIn(WORKTREE_RULE.rstrip(), markdown)
        self.assertIn("Deterministic transport fixture; no semantic-quality claim.", markdown)
        self.assertIn("Synthetic missing evidence.", markdown)
        self.assertIn("预期", markdown)
        self.assertIn("实际", markdown)
        latest = json.loads((self.state / "latest-rules-check.json").read_text())
        self.assertIn(str(Path(latest["run"]) / "report.md"), result.stdout)
        self.assertIn(str(Path(latest["run"]) / "result.json"), result.stdout)

    def test_reason_audit_cannot_rewrite_a_correct_blind_verdict(self):
        self.set_mode("audit_mismatch")
        result = self.check_rules()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = self.latest_result()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(all(item["actual"] == item["expected"] for item in report["samples"]))
        self.assertTrue(report["audits"])

    def test_missing_or_invalid_reason_audit_evidence_is_incomplete(self):
        for mode in ("audit_missing_check", "audit_duplicate_check", "audit_bad_quote",
                     "audit_wrong_case", "audit_incomplete"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                result = self.check_rules()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.latest_result()["status"], "incomplete")

    def test_rules_or_case_changes_during_model_calls_invalidate_the_run(self):
        for mode in ("change_rules", "change_case", "change_line_endings"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                result = self.check_rules()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.latest_result()["status"], "incomplete")

    def test_missing_rules_or_labeled_fragment_fails_without_any_model_call(self):
        rules = self.repo / ".clarity" / "current" / "rules.md"
        rules.unlink()
        result = self.check_rules()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])
        self.write(".clarity/current/rules.md", WORKTREE_RULE)
        self.write(".clarity/current/cases/0007. missing-fragment.md", "# Empty fixture\n\n## 正例\nNo supplied fragment.\n\n## 反例\nNo supplied fragment.\n")
        result = self.check_rules()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_non_object_configurations_are_reported_incomplete_without_a_traceback(self):
        for configuration in (None, [], "configuration-string", 3):
            with self.subTest(configuration=configuration):
                self.config_path.write_text(json.dumps(configuration))
                result = self.check_rules()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.latest_result()["status"], "incomplete")
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertEqual(self.calls(), [])

    def test_a_relative_repo_from_a_subdirectory_uses_the_git_root_materials(self):
        subdirectory = self.repo / "nested" / "working-directory"
        subdirectory.mkdir(parents=True)
        result = self.execute([sys.executable, str(RUNNER / "review.py"), "check-rules",
                               "--repo", ".", "--config", str(self.config_path)],
                              cwd=subdirectory, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.latest_result()["status"], "passed")
        self.assertEqual(len(self.latest_result()["samples"]), self.sample_count)
        self.assertFalse((subdirectory / ".clarity").exists())

    def test_an_invalid_repo_is_incomplete_and_does_not_change_daily_review_state(self):
        self.state.mkdir(parents=True)
        previous_daily = b'{"run":"untouched-daily-review"}\n'
        (self.state / "latest.json").write_bytes(previous_daily)
        outside = self.root / "not-a-repository"
        outside.mkdir()
        result = self.execute([sys.executable, str(RUNNER / "review.py"), "check-rules",
                               "--repo", str(outside), "--config", str(self.config_path)], check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual((self.state / "latest.json").read_bytes(), previous_daily)
        self.assertFalse((outside / ".clarity").exists())
        self.assertFalse((outside / ".git").exists())
        self.assertEqual(self.calls(), [])


class SampleCollectionTests(unittest.TestCase):
    """SampleCollectionTests() 独立核对片段的固定预期和事实输入，防止漏审及答案泄漏。"""

    def test_existing_handoff_and_failure_fixtures_keep_necessary_facts_and_source_mapping(self):
        necessary = {
            "0003. comment-rule-repetition.md": [
                "本例中，05 是移交前的负责方；写入“已提交”后，Slot 的释放责任已交给后续任务。"],
            "0004. stop-after-result-write-failure.md": [
                "适用条件是代码捕获结果记录失败后，仍会检查并尝试处理 `result.stop`。",
                "下列注释均位于 `if (result.stop)` 前。",
                "本例中的 STOP 处理已经存在，不是尚待补上的行为。",
                "本例给出的是仍会尝试处理的事实，没有给出保护已成功的证据。"],
            "0005. remote-facts-after-local-failure.md": [
                "适用条件是已经确认创建并取得 LinkedIn Job ID，随后本地记录失败。",
                "下列注释均位于构造并抛出本地记录失败异常的接点。"],
        }
        for name, facts in necessary.items():
            source = CASES / name
            if not source.exists():
                continue
            with self.subTest(fixture=name):
                path, document = ".clarity/current/cases/" + name, source.read_text()
                samples = collect_samples({path: document})
                self.assertTrue(samples)
                for sample in samples:
                    for fact in facts:
                        self.assertIn(fact, sample["context"])
                    self.assertTrue(any(context.startswith("检查目标：") for context in sample["context"]))
                    for context in sample["context"]:
                        self.assertIn(context.removeprefix("检查目标："), document)
                    first = sample["case_line"] - 1
                    original = document.splitlines()[first:first + len(sample["content"].splitlines())]
                    if all(re.match(r"^\s*>\s?", line) for line in original):
                        original = [re.sub(r"^\s*>\s?", "", line) for line in original]
                    self.assertEqual("\n".join(original), sample["content"])

    def test_neutral_group_premises_follow_ancestors_without_sibling_sample_explanations(self):
        document = """# CASE_TITLE_SECRET

适用条件是 COMMON_CONTEXT_MARKER。

## 目的

检查限定片段的说明 SCOPE_CONTEXT_MARKER。

## 第一组

适用条件是 FIRST_GROUP_CONTEXT_MARKER。

### 第一子组

本例中，FIRST_CHILD_CONTEXT_MARKER 是当前接点的已知条件。

#### 正例：FIRST_LABEL_SECRET

适用条件是 FIRST_SAMPLE_PRIVATE_SECRET。

```text
GOOD_A
```

适用条件是 FIRST_EXPLANATION_SECRET。

#### 反例

```text
BAD_A
```

### 第二子组

本例中，SECOND_CHILD_CONTEXT_MARKER 是当前接点的已知条件。

#### 正例

```text
GOOD_B
```

#### 反例

```text
BAD_B
```

## 第二组

适用条件是 SECOND_GROUP_CONTEXT_MARKER。

### 正例

```text
GOOD_C
```

### 反例

```text
BAD_C
```
"""
        samples = collect_samples({".clarity/current/cases/scoped.md": document})
        common = ["适用条件是 COMMON_CONTEXT_MARKER。",
                  "检查目标：检查限定片段的说明 SCOPE_CONTEXT_MARKER。"]
        expected = {
            "GOOD_A": [*common, "适用条件是 FIRST_GROUP_CONTEXT_MARKER。",
                       "本例中，FIRST_CHILD_CONTEXT_MARKER 是当前接点的已知条件。"],
            "BAD_A": [*common, "适用条件是 FIRST_GROUP_CONTEXT_MARKER。",
                      "本例中，FIRST_CHILD_CONTEXT_MARKER 是当前接点的已知条件。"],
            "GOOD_B": [*common, "适用条件是 FIRST_GROUP_CONTEXT_MARKER。",
                       "本例中，SECOND_CHILD_CONTEXT_MARKER 是当前接点的已知条件。"],
            "BAD_B": [*common, "适用条件是 FIRST_GROUP_CONTEXT_MARKER。",
                      "本例中，SECOND_CHILD_CONTEXT_MARKER 是当前接点的已知条件。"],
            "GOOD_C": [*common, "适用条件是 SECOND_GROUP_CONTEXT_MARKER。"],
            "BAD_C": [*common, "适用条件是 SECOND_GROUP_CONTEXT_MARKER。"],
        }
        self.assertEqual({sample["content"]: sample["context"] for sample in samples}, expected)
        self.assertTrue(all("SECRET" not in "\n".join(sample["context"]) for sample in samples))

    def test_facts_are_kept_as_source_sentences_without_adjacent_judgment_or_answers(self):
        document = """# CASE_TITLE_SECRET

## 目的

检查本地失败后仍会尝试的保护 SCOPE_CONTEXT_MARKER。

## 判断理由

适用条件是 REASON_SECRET。当前 REASON_FACT_SECRET 已发生。

## 边界

适用条件是 ACTUAL_FACT_CONTEXT_MARKER。下列注释均位于 LOCATION_CONTEXT_MARKER 前。这里应按 GOLD_REASON_SECRET 判为通过。
当前 CURRENT_FACT_CONTEXT_MARKER 仍只保存在内存中。正例通过、反例拒绝 ANSWER_SECRET。
注入的依赖是否另有日志，仍需核实。审查时不能直接断言 WHOLE_SYSTEM_SECRET。
如果已核实 LEGACY_FACT_CONTEXT_MARKER，那么这个片段应判为 LEGACY_ANSWER_SECRET。

## 验收

当前 ACCEPTANCE_FACT_SECRET 已满足，正例应通过。

## 正例

```text
GOOD
```

当前 POSITIVE_EXPLANATION_SECRET 已满足。

## 反例

> BAD

当前 NEGATIVE_EXPLANATION_SECRET 已满足。
"""
        samples = collect_samples({".clarity/current/cases/sentences.md": document})
        expected = ["检查目标：检查本地失败后仍会尝试的保护 SCOPE_CONTEXT_MARKER。",
                    "适用条件是 ACTUAL_FACT_CONTEXT_MARKER。",
                    "下列注释均位于 LOCATION_CONTEXT_MARKER 前。",
                    "当前 CURRENT_FACT_CONTEXT_MARKER 仍只保存在内存中。",
                    "注入的依赖是否另有日志，仍需核实。",
                    "如果已核实 LEGACY_FACT_CONTEXT_MARKER"]
        self.assertEqual([(sample["content"], sample["expected"]) for sample in samples],
                         [("GOOD", "accept"), ("BAD", "reject")])
        for sample in samples:
            self.assertEqual(sample["context"], expected)
            self.assertNotIn("SECRET", "\n".join(sample["context"]))

    def test_metadata_changes_do_not_change_blind_material_but_premise_changes_do(self):
        document = """# ORIGINAL_TITLE_SECRET

来源：ORIGINAL_SOURCE_SECRET

## 目的

检查限定片段的职责说明 SCOPE_CONTEXT_MARKER。

## 边界

本例中，05 是 FACT_CONTEXT_MARKER 的移交方。

## 正例：ORIGINAL_POSITIVE_SECRET

```text
GOOD
```

ORIGINAL_EXPLANATION_SECRET

## 反例：ORIGINAL_NEGATIVE_SECRET

```text
BAD
```

## 判断理由

ORIGINAL_REASON_SECRET
"""
        def blind_material(path, text):
            return [(sample["content"], sample["context"])
                    for sample in collect_samples({path: text})]

        original = blind_material(".clarity/current/cases/original-name.md", document)
        renamed = blind_material(".clarity/current/cases/renamed.md", document.replace("ORIGINAL_", "RENAMED_"))
        self.assertEqual(renamed, original)
        self.assertEqual(original, [("GOOD", ["检查目标：检查限定片段的职责说明 SCOPE_CONTEXT_MARKER。",
                                             "本例中，05 是 FACT_CONTEXT_MARKER 的移交方。"]),
                                    ("BAD", ["检查目标：检查限定片段的职责说明 SCOPE_CONTEXT_MARKER。",
                                             "本例中，05 是 FACT_CONTEXT_MARKER 的移交方。"])])
        changed = blind_material(".clarity/current/cases/original-name.md",
                                 document.replace("FACT_CONTEXT_MARKER", "CHANGED_FACT_CONTEXT_MARKER"))
        self.assertNotEqual(changed, original)
        self.assertEqual([content for content, _ in changed], [content for content, _ in original])

    def test_locator_titles_with_judgment_words_keep_frozen_sample_facts(self):
        titles = {
            "0006. business-choice-rationale.md": "# 业务选择要说明理由，不能只复述动作",
            "0007. todo-implementation-status.md": "# 待实现工作的完成状态判定",
        }
        for name, title in titles.items():
            with self.subTest(fixture=name):
                source = CASES / name
                document = source.read_text()
                path = ".clarity/current/cases/" + name
                original = collect_samples({path: document})
                renamed = collect_samples({path: title + "\n" + document.split("\n", 1)[1]})
                for sample in renamed:
                    sample["case_title"] = original[0]["case_title"]
                self.assertEqual(renamed, original)
                self.assertTrue(all(sample["context"] for sample in original))

    def test_pair_container_facts_stay_with_its_descendants_and_not_sibling_groups(self):
        document = """# 样例判定的固定材料

## 目的

检查局部状态说明。

## 说明理由的第一组

### 正反例

适用条件是 FIRST_GROUP_CONTEXT_MARKER。

#### 正例

```text
GOOD_A
```

#### 反例

> BAD_A

## 第二组

适用条件是 SECOND_GROUP_CONTEXT_MARKER。

### 正例

> GOOD_B

### 反例

```text
BAD_B
```
"""
        samples = collect_samples({".clarity/current/cases/pairs.md": document})
        common = "检查目标：检查局部状态说明。"
        self.assertEqual({sample["content"]: sample["context"] for sample in samples}, {
            "GOOD_A": [common, "适用条件是 FIRST_GROUP_CONTEXT_MARKER。"],
            "BAD_A": [common, "适用条件是 FIRST_GROUP_CONTEXT_MARKER。"],
            "GOOD_B": [common, "适用条件是 SECOND_GROUP_CONTEXT_MARKER。"],
            "BAD_B": [common, "适用条件是 SECOND_GROUP_CONTEXT_MARKER。"],
        })

    def test_explicit_label_wins_when_its_description_mentions_the_other_label(self):
        document = """# 固定案例

## 正例：补足反例缺少的说明

```text
GOOD
```

## 反例一：与正例相比缺少说明

```text
BAD
```
"""
        samples = collect_samples({".clarity/current/cases/labels.md": document})
        self.assertEqual([(sample["content"], sample["expected"]) for sample in samples],
                         [("GOOD", "accept"), ("BAD", "reject")])

    def test_audit_section_subtrees_do_not_add_samples_and_the_next_section_resumes(self):
        for title in ("所有者原文（完整保留）", "来源", "判断理由", "验收"):
            with self.subTest(title=title):
                document = f"""# 固定案例

## 第一组

### 正例

> GOOD_A

#### {title}

适用条件是 AUDIT_FACT_SECRET。

##### 正例

> AUDIT_GOOD_SECRET

##### 反例

```text
AUDIT_BAD_SECRET
```

#### 继续说明

> GOOD_DETAIL

### 反例

> BAD_A

## {title}

### 正例

> ORIGINAL_GOOD_SECRET

### 反例

> ORIGINAL_BAD_SECRET

## 正常的下一组

### 正例

> GOOD_B

### 反例

> BAD_B
"""
                samples = collect_samples({".clarity/current/cases/audit-sections.md": document})
                self.assertEqual([(sample["content"], sample["expected"]) for sample in samples],
                                 [("GOOD_A", "accept"), ("GOOD_DETAIL", "accept"), ("BAD_A", "reject"),
                                  ("GOOD_B", "accept"), ("BAD_B", "reject")])
                self.assertTrue(all("SECRET" not in "\n".join(sample["context"]) for sample in samples))

    def test_explicit_fixed_answers_mixed_into_scope_or_supported_facts_are_incomplete(self):
        template = """# Synthetic fixture

## 目的

{scope}

## 边界

{fact}

## 正例

```text
GOOD
```

## 反例

```text
BAD
```
"""
        answers = ("正例应通过，反例应拒绝。", "GOLD=true。", "预期答案是通过。",
                   "预期判断为 accept。", "预期结果为 reject。", "当前固定预期是拒绝。")
        for target in ("scope", "fact"):
            for answer in answers:
                with self.subTest(target=target, answer=answer):
                    values = {"scope": "检查限定片段的当前状态。", "fact": "当前结果只保存在内存中。"}
                    values[target] = (("检查限定片段的当前状态，" if target == "scope"
                                       else "当前结果只保存在内存中，") + answer)
                    with self.assertRaises(CheckIncomplete):
                        collect_samples({".clarity/current/cases/mixed-answer.md": template.format(**values)})

    def test_plain_labels_keep_shared_scope_without_samples_from_the_original_copy(self):
        document = """# CASE_TITLE_SECRET

## 目的

检查局部职责说明 SCOPE_CONTEXT_MARKER。

## 边界

适用条件是 FACT_CONTEXT_MARKER。

## 正反例

正例：PLAIN_LABEL_SECRET

~~~text
GOOD
~~~

反例：OTHER_LABEL_SECRET

> BAD

## 所有者原文

````markdown
正例：ORIGINAL_LABEL_SECRET

```text
ORIGINAL_CONTENT_SECRET
```
````
"""
        samples = collect_samples({".clarity/current/cases/plain.md": document})
        self.assertEqual([(sample["content"], sample["expected"]) for sample in samples],
                         [("GOOD", "accept"), ("BAD", "reject")])
        for sample in samples:
            self.assertEqual(sample["context"],
                             ["检查目标：检查局部职责说明 SCOPE_CONTEXT_MARKER。",
                              "适用条件是 FACT_CONTEXT_MARKER。"])

    def test_empty_labeled_leaf_after_complete_pair_is_incomplete(self):
        document = """# Synthetic fixture

## 正例

```text
GOOD
```

## 反例

```text
BAD
```
"""
        for title in ("正例二", "反例二"):
            with self.subTest(title=title):
                with self.assertRaisesRegex(CheckIncomplete, "正反区段没有可独立判定"):
                    collect_samples({".clarity/current/cases/empty-leaf.md": document + "\n## " + title + "\n"})

    def test_labeled_parent_without_eligible_fragment_is_incomplete_at_peer_or_end(self):
        complete_pair = "# 固定案例\n\n## 正例\n\n> GOOD\n\n## 反例\n\n> BAD\n"
        next_peer = "\n## 下一组\n\n### 正例\n\n> GOOD_NEXT\n\n### 反例\n\n> BAD_NEXT\n"
        path = ".clarity/current/cases/empty-labeled-parent.md"
        for title, opposite in (("正例二", "反例"), ("反例二", "正例")):
            children = {
                "source_only": "### 来源\n\n> HISTORICAL_ONLY\n",
                "prose_only": "### 补充细节\n\n这里只提供说明，没有独立待判片段。\n",
                "opposite_only": f"### {opposite}\n\n> OPPOSITE_ONLY\n",
            }
            for kind, child in children.items():
                for boundary, suffix in (("end", ""), ("peer", next_peer)):
                    with self.subTest(parent=title, child=kind, boundary=boundary):
                        document = complete_pair + "\n## " + title + "\n\n" + child + suffix
                        line = document.splitlines().index("## " + title) + 1
                        with self.assertRaisesRegex(CheckIncomplete,
                                                    re.escape(f"{path}:{line}: 正反区段没有可独立判定")):
                            collect_samples({path: document})

    def test_labeled_parent_keeps_matching_descendant_after_an_opposite_subscope(self):
        complete_pair = "# 固定案例\n\n## 正例\n\n> GOOD\n\n## 反例\n\n> BAD\n"
        for title, expected, opposite, other_expected in (("正例", "accept", "反例", "reject"),
                                                          ("反例", "reject", "正例", "accept")):
            for matching_title in ("补充细节", title):
                with self.subTest(parent=title, matching_child=matching_title):
                    document = (complete_pair + f"\n## {title}二\n\n### {opposite}\n\n> OPPOSITE\n"
                                + f"\n### {matching_title}\n\n> MATCHING\n")
                    samples = collect_samples({".clarity/current/cases/covered-parent.md": document})
                    self.assertEqual([(sample["content"], sample["expected"]) for sample in samples],
                                     [("GOOD", "accept"), ("BAD", "reject"),
                                      ("OPPOSITE", other_expected), ("MATCHING", expected)])

    def test_leading_blank_lines_in_fences_keep_exact_original_source_mapping(self):
        document = """# 固定案例

## 正例

```text


GOOD

```

## 反例

```text

BAD
```
"""
        samples = collect_samples({".clarity/current/cases/blank-lines.md": document})
        self.assertEqual([(sample["content"], sample["expected"]) for sample in samples],
                         [("GOOD", "accept"), ("BAD", "reject")])
        for sample in samples:
            first = sample["case_line"] - 1
            self.assertEqual(document.splitlines()[first:first + len(sample["content"].splitlines())],
                             sample["content"].splitlines())

    def test_whitespace_only_blockquotes_do_not_become_samples(self):
        for blank in (">", "> ", "> \n> \t"):
            with self.subTest(blank=blank):
                document = "# 固定案例\n\n## 正例\n\n> GOOD\n\n## 反例\n\n" + blank + "\n"
                with self.assertRaisesRegex(CheckIncomplete, "正反区段没有可独立判定"):
                    collect_samples({".clarity/current/cases/empty-quote.md": document})

    def test_empty_labeled_parent_body_keeps_material_in_unlabeled_children(self):
        document = """# Synthetic fixture

## 正例

### 补充细节

```text
GOOD
```

## 反例

### 补充细节

```text
BAD
```
"""
        samples = collect_samples({".clarity/current/cases/empty-parent.md": document})
        self.assertEqual([(sample["content"], sample["expected"]) for sample in samples],
                         [("GOOD", "accept"), ("BAD", "reject")])

    def test_language_order_in_boundary_reaches_context_without_judgment_or_answers(self):
        for language in ("本例说明按每句中文、英文的顺序排列。",
                         "本例说明按每句英文、中文、西班牙文的顺序排列。",
                         "示例的语言顺序为中文、英文。"):
            with self.subTest(language=language):
                document = f"""# CASE_TITLE_SECRET

## 目的

检查限定片段的语言组织 SCOPE_CONTEXT_MARKER。

## 判断依据

JUDGMENT_SECRET 一项待办只能有一个公共标记。

## 边界

- {language} BOUNDARY_REASON_SECRET 不属于事实前提。

## 验收

ANSWER_SECRET 正例应通过，反例应拒绝。

## 正反例

### 正例

```text
GOOD
```

### 反例

```text
BAD
```
"""
                samples = collect_samples({".clarity/current/cases/language-boundary.md": document})
                self.assertEqual([(sample["content"], sample["expected"]) for sample in samples],
                                 [("GOOD", "accept"), ("BAD", "reject")])
                for sample in samples:
                    self.assertEqual(sample["context"],
                                     ["检查目标：检查限定片段的语言组织 SCOPE_CONTEXT_MARKER。", language])

    def test_unlabeled_subheading_inherits_the_enclosing_positive_expectation(self):
        document = """# Synthetic fixture

## 正例

```text
GOOD_A
```

### 补充细节

```text
GOOD_B
```

## 反例

```text
BAD_C
```
"""
        samples = collect_samples({".clarity/current/rules.md": WORKTREE_RULE,
                                   ".clarity/current/cases/nested.md": document})
        self.assertEqual([(sample["content"], sample["expected"]) for sample in samples],
                         [("GOOD_A", "accept"), ("GOOD_B", "accept"), ("BAD_C", "reject")])
        for sample in samples:
            self.assertEqual(document.splitlines()[sample["case_line"] - 1], sample["content"])


if __name__ == "__main__":
    unittest.main()
