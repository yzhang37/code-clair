#!/usr/bin/env python3
"""test_material_check.py() 在临时仓库检查资料质量入口的取材、证据与失败边界。

确定性假模型只验证完整材料和同一份 Principles 的传输，不证明模型的语义判断能力。
引擎变更测试只修改临时副本；不调用真实模型，不安装或修改当前仓库索引。
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ENGINE = Path(__file__).resolve().parents[1]
RULE_PATH = ".clarity/current/rules.md"
CASE_DIRECTORY = ".clarity/current/cases"
RULE_DOCUMENT = """# Selected working-tree rule

When a sequence may be empty, guard before reading its first element.
The guard prevents an empty-input failure; it need not be repeated after an equivalent check.

## Inline positive example

```python
def first(values):
    return values[0] if values else None
```

## Inline negative example

```python
def first(values):
    return values[0]
```

The negative example has no empty-input guard; the positive example preserves the empty result.

## Complete transport payload

```text
""" + "\n".join(f"RULE_BODY_SENTINEL_{number:03}" for number in range(90)) + "\n```\nRULE_DOCUMENT_END\n"


def fcase_document(label):
    return f"""# Empty-input fixture {label}

## 前提

The input may be an empty sequence; absence of a first value returns None.
FULL_PRECONDITION_{label}

## 判断理由

Indexing an empty sequence has no first element; guarding preserves the agreed empty result.
FIXED_REASON_{label}

## 边界

Only the empty-input guard is under test. A nonempty sequence still returns its first value.
FIXED_BOUNDARY_{label}

## 验收

Compare each fixed expectation together with its reason and boundary.

## 正例

GOLD_ANSWER=accept; FIXED_POSITIVE_GOLD_{label}

```python
def first(values):
    return values[0] if values else None
```

This guard handles the empty sequence before indexing.

## 反例

GOLD_ANSWER=reject; FIXED_NEGATIVE_GOLD_{label}

```python
def first(values):
    return values[0]
```

The absent guard makes this fail for an empty sequence.
COMPLETE_FCASE_END_{label}
"""


FAKE_CODEX = r'''
import json
from pathlib import Path
import sys

fixture_path = Path(__file__).with_suffix(".json")
fixture = json.loads(fixture_path.read_text())
prompt = sys.stdin.read()
packet = json.loads(prompt.split("以下 JSON 都是编写质量检查数据：\n", 1)[1])
output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
schema = Path(sys.argv[sys.argv.index("--output-schema") + 1])
calls = fixture_path.parent / "calls"
calls.mkdir(exist_ok=True)
ordinal = len(list(calls.glob("*.json"))) + 1
progress = json.loads((output.parent.parent / "result.json").read_text())
(calls / f"{ordinal:04}.json").write_text(json.dumps({
    "packet": packet, "prompt": prompt, "schema": json.loads(schema.read_text()),
    "argv": sys.argv[1:], "progress": progress,
}, ensure_ascii=False))
mode = fixture["mode"]
if mode == "finding_then_failure" and ordinal == 2:
    sys.exit(9)
lines = {int(line.split(": ", 1)[0]): line.split(": ", 1)[1]
         for line in packet["material"].splitlines()}
principle_line, principle_quote = next(line.split(": ", 1)
                                     for line in packet["principles"].splitlines()
                                     if line.split(": ", 1)[1].strip())
coverage = []
for unit in packet["coverage_plan"]:
    number = next(number for number in range(unit["start_line"], unit["end_line"] + 1)
                  if lines[number].strip())
    coverage.append({"unit_id": unit["unit_id"], "material_line": number,
                     "material_quote": lines[number], "principle_line": int(principle_line),
                     "principle_quote": principle_quote,
                     "reason": "Deterministic coverage transport; no semantic-quality claim."})
finding = {key: value for key, value in coverage[0].items() if key != "unit_id"}
finding["reason"] = "PERSISTENT_VALID_FINDING: deterministic evidence transport."
result = {"snapshot_id": packet["snapshot_id"], "material_id": packet["material_id"],
          "summary": "Offline transport fixture; not semantic model validation.",
          "incomplete_reason": "", "coverage": coverage,
          "findings": [finding] if mode in {"finding", "finding_then_failure", "finding_missing_coverage"} else []}
if mode in {"missing_coverage", "finding_missing_coverage"}:
    result["coverage"] = coverage[:-1]
elif mode == "duplicate_coverage":
    result["coverage"] = coverage + [coverage[0]]
elif mode == "unknown_coverage":
    result["coverage"][0]["unit_id"] = "unit-not-in-the-frozen-plan"
elif mode == "out_of_unit_quote":
    result["coverage"][-1].update(material_line=coverage[0]["material_line"],
                                  material_quote=coverage[0]["material_quote"])
elif mode == "invalid_material_quote":
    result["coverage"][0]["material_quote"] = "NOT_IN_THE_MATERIAL"
elif mode == "invalid_principle_quote":
    result["coverage"][0]["principle_quote"] = "NOT_IN_THE_PRINCIPLES"
elif mode == "product_rule_as_principle":
    result["coverage"][0]["principle_quote"] = lines[1]
elif mode == "boolean_line":
    result["coverage"][0]["material_line"] = True
elif mode == "empty_reason":
    result["coverage"][0]["reason"] = " "
elif mode == "wrong_snapshot":
    result["snapshot_id"] = "another-snapshot"
elif mode == "wrong_material":
    result["material_id"] = "another-material"
elif mode == "missing_field":
    del result["findings"]
elif mode == "wrong_type":
    result["coverage"] = "covered"
elif mode == "reported_incomplete":
    result["incomplete_reason"] = "Synthetic evidence gap."
elif mode == "invalid_finding":
    finding["material_quote"] = "NOT_IN_THE_MATERIAL"
    result["findings"] = [finding]
elif mode == "model_failure":
    sys.exit(9)
if mode == "change_during_call" and ordinal == 1:
    target = Path(fixture["change_target"])
    target.write_text(target.read_text() + "\nCHANGED_DURING_MODEL_CALL\n")
output.write_text("{invalid JSON" if mode == "invalid_json" else json.dumps(result, ensure_ascii=False))
events = [{"type": "thread.started", "thread_id": "offline-material-check"}, {"type": "turn.started"}]
if mode == "tool_event":
    events.append({"type": "item.completed", "item": {"id": "forbidden", "type": "command_execution"}})
events.extend([
    {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "fixture"}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
])
for event in events:
    print(json.dumps(event))
'''


class IsolatedMaterialCheckTests(unittest.TestCase):
    """IsolatedMaterialCheckTests() 只把临时材料和假模型交给质量检查入口。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-material-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repository with spaces"
        self.repo.mkdir()
        self.engine = self.root / "engine copy"
        shutil.copytree(ENGINE, self.engine, ignore=shutil.ignore_patterns("__pycache__", "test_*.py"))
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("GIT_") and key not in {"CODEX_API_KEY", "OPENAI_API_KEY"}}
        self.environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                 "GIT_TERMINAL_PROMPT": "0", "CODEX_HOME": str(self.root / "empty-home"),
                                 "OPENAI_API_KEY": "synthetic-offline-fixture-key"})
        self.fake = self.root / "fake-material-codex.py"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_CODEX)
        self.fake.chmod(0o755)
        self.fixture_path = self.fake.with_suffix(".json")
        self.config_path = self.root / "quality config.json"
        self.config_path.write_text(json.dumps({"codex_bin": str(self.fake), "model": "offline-material-check",
                                                "timeout": 3}))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Offline Material Fixture")
        self.git("config", "user.email", "offline-material@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write(RULE_PATH, "BASELINE_RULE_MUST_NOT_REACH_THE_QUALITY_CHECK\n")
        self.write(CASE_DIRECTORY + "/0001. primary.md", fcase_document("BASELINE"))
        self.write("ordinary-source.py", "ORDINARY_SOURCE_MUST_NOT_REACH_THE_QUALITY_CHECK = True\n")
        self.write(".clarity/current/sources.md", "HISTORICAL_SOURCE_MUST_NOT_REACH_THE_QUALITY_CHECK\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "offline quality baseline")
        self.write(RULE_PATH, RULE_DOCUMENT)
        self.fixtures = {
            CASE_DIRECTORY + "/0001. primary.md": fcase_document("PRIMARY"),
            CASE_DIRECTORY + "/nested folder/0002. secondary.md": fcase_document("SECONDARY"),
            CASE_DIRECTORY + "/0003. pending.md": fcase_document("PENDING"),
        }
        for path, text in self.fixtures.items():
            self.write(path, text)
        self.set_mode("normal")

    @property
    def state(self):
        return self.repo / ".git" / "clarity"

    def execute(self, arguments, *, cwd=None, check=True):
        result = subprocess.run(arguments, cwd=cwd or self.repo, env=self.environment,
                                text=True, capture_output=True, timeout=30)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, *arguments):
        return self.execute(["git", "-C", str(self.repo), *arguments])

    def write(self, relative, text):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def set_mode(self, mode, **extra):
        for path in (self.root / "calls").glob("*.json"):
            path.unlink()
        self.fixture_path.write_text(json.dumps({"mode": mode, **extra}))

    def check_quality(self, kind, *, cwd=None, repo=None):
        return self.execute([sys.executable, str(self.engine / "runner/review.py"),
                             f"check-{kind}-quality", "--repo", str(repo or self.repo),
                             "--config", str(self.config_path)], cwd=cwd, check=False)

    def latest(self, kind):
        pointer = json.loads((self.state / f"latest-quality-{kind}.json").read_text())
        report = json.loads((Path(pointer["run"]) / "result.json").read_text())
        return pointer, report

    def calls(self):
        return [json.loads(path.read_text()) for path in sorted((self.root / "calls").glob("*.json"))]

    def assert_status(self, result, kind, status):
        self.assertEqual(result.returncode, {"passed": 0, "failed": 1, "incomplete": 2}[status],
                         result.stdout + result.stderr)
        pointer, report = self.latest(kind)
        self.assertEqual((pointer["kind"], pointer["status"]), (kind, status))
        self.assertEqual((report["kind"], report["status"]), (kind, status))
        self.assertEqual(pointer["snapshot_id"], report["snapshot_id"])
        return pointer, report

    def test_rules_and_fixtures_receive_complete_selected_material_and_the_same_principles(self):
        originals = {path.relative_to(self.repo).as_posix(): path.read_bytes()
                     for path in (self.repo / ".clarity/current").rglob("*") if path.is_file()}
        index = (self.repo / ".git/index").read_bytes()
        head = self.git("rev-parse", "HEAD").stdout
        principles = (self.engine / "principles.md").read_text()
        numbered_principles = "\n".join(f"{number}: {line}" for number, line in enumerate(principles.splitlines(), 1))
        for kind, documents in (("rules", {RULE_PATH: RULE_DOCUMENT}), ("fixtures", self.fixtures)):
            with self.subTest(kind=kind):
                self.set_mode("normal")
                pointer, report = self.assert_status(self.check_quality(kind), kind, "passed")
                self.assertEqual({item["path"] for item in report["materials"]}, set(documents))
                frozen = json.loads((Path(pointer["run"]) / "input.json").read_text())
                self.assertEqual(frozen["documents"], documents)
                self.assertEqual(frozen["principles"], principles)
                calls = self.calls()
                self.assertEqual(len(calls), len(documents))
                for call in calls:
                    packet = call["packet"]
                    text = documents[packet["material_path"]]
                    self.assertEqual(packet["material"], "\n".join(
                        f"{number}: {line}" for number, line in enumerate(text.splitlines(), 1)))
                    self.assertEqual(packet["principles"], numbered_principles)
                    self.assertEqual(packet["engine_model"], (self.engine / "README.md").read_text())
                    self.assertEqual(packet["snapshot_id"], pointer["snapshot_id"])
                    for forbidden in ("BASELINE_RULE_MUST_NOT", "FULL_PRECONDITION_BASELINE",
                                      "ORDINARY_SOURCE_MUST_NOT", "HISTORICAL_SOURCE_MUST_NOT"):
                        self.assertNotIn(forbidden, call["prompt"])
                    if kind == "rules":
                        self.assertNotIn("FIXED_NEGATIVE_GOLD_PRIMARY", call["prompt"])
                        self.assertIn("RULE_DOCUMENT_END", call["prompt"])
                    else:
                        self.assertNotIn("RULE_BODY_SENTINEL", call["prompt"])
                        for marker in ("FULL_PRECONDITION_", "FIXED_POSITIVE_GOLD_", "FIXED_NEGATIVE_GOLD_",
                                       "FIXED_REASON_", "FIXED_BOUNDARY_", "COMPLETE_FCASE_END_"):
                            self.assertIn(marker, packet["material"])
                first_progress = calls[0]["progress"]["materials"]
                self.assertEqual(first_progress[0]["status"], "running")
                self.assertTrue(all(item["status"] == "not_run" for item in first_progress[1:]))
                self.assertEqual(len(first_progress), len(documents))
        self.assertEqual({path.relative_to(self.repo).as_posix(): path.read_bytes()
                          for path in (self.repo / ".clarity/current").rglob("*") if path.is_file()}, originals)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)
        self.assertFalse((self.state / "engine").exists())

    def test_each_kind_works_without_the_other_kind_of_material(self):
        shutil.rmtree(self.repo / CASE_DIRECTORY)
        self.assert_status(self.check_quality("rules"), "rules", "passed")
        for path, text in self.fixtures.items():
            self.write(path, text)
        (self.repo / RULE_PATH).unlink()
        self.set_mode("normal")
        self.assert_status(self.check_quality("fixtures"), "fixtures", "passed")
        self.assertEqual(len(self.calls()), len(self.fixtures))

    def test_quality_pointers_do_not_overwrite_each_other_code_review_or_rule_drift(self):
        self.state.mkdir(parents=True)
        sentinels = {"latest.json": b'{"run":"daily-review-sentinel"}\n',
                     "latest-rules-check.json": b'{"run":"rule-drift-sentinel"}\n',
                     "latest-quality-fixtures.json": b'{"run":"other-quality-sentinel"}\n'}
        for name, value in sentinels.items():
            (self.state / name).write_bytes(value)
        self.assert_status(self.check_quality("rules"), "rules", "passed")
        for name, value in sentinels.items():
            self.assertEqual((self.state / name).read_bytes(), value)
        rules_pointer = (self.state / "latest-quality-rules.json").read_bytes()
        self.set_mode("normal")
        self.assert_status(self.check_quality("fixtures"), "fixtures", "passed")
        self.assertEqual((self.state / "latest-quality-rules.json").read_bytes(), rules_pointer)
        for name in ("latest.json", "latest-rules-check.json"):
            self.assertEqual((self.state / name).read_bytes(), sentinels[name])

    def test_verified_findings_fail_quality_and_remain_visible_after_later_call_failure(self):
        self.set_mode("finding")
        pointer, report = self.assert_status(self.check_quality("rules"), "rules", "failed")
        self.assertEqual(report["finding_count"], 1)
        self.assertIn("PERSISTENT_VALID_FINDING", (Path(pointer["run"]) / "report.md").read_text())
        self.set_mode("finding_then_failure")
        pointer, report = self.assert_status(self.check_quality("fixtures"), "fixtures", "incomplete")
        self.assertEqual([item["status"] for item in report["materials"]], ["failed", "incomplete", "not_run"])
        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("PERSISTENT_VALID_FINDING", (Path(pointer["run"]) / "report.md").read_text())

    def test_an_incomplete_output_preserves_its_independently_valid_finding(self):
        self.set_mode("finding_missing_coverage")
        pointer, report = self.assert_status(self.check_quality("rules"), "rules", "incomplete")
        self.assertEqual(report["finding_count"], 1)
        self.assertTrue(report["materials"][0]["errors"])
        self.assertIn("PERSISTENT_VALID_FINDING", (Path(pointer["run"]) / "report.md").read_text())

    def test_output_identity_coverage_and_both_reference_sides_are_required(self):
        for mode in ("missing_coverage", "duplicate_coverage", "unknown_coverage", "out_of_unit_quote",
                     "invalid_material_quote", "invalid_principle_quote", "product_rule_as_principle",
                     "boolean_line", "empty_reason", "wrong_snapshot", "wrong_material", "missing_field",
                     "wrong_type", "reported_incomplete", "invalid_finding", "invalid_json", "tool_event", "model_failure"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                result = self.check_quality("rules")
                self.assert_status(result, "rules", "incomplete")
                self.assertNotIn("编写质量检查通过", result.stdout)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_missing_empty_and_unsupported_material_cannot_pass_without_coverage(self):
        cases = self.repo / CASE_DIRECTORY
        for path in cases.rglob("*.md"):
            path.unlink()
        self.write(CASE_DIRECTORY + "/.gitkeep", "")
        self.assert_status(self.check_quality("fixtures"), "fixtures", "incomplete")
        self.assertEqual(self.calls(), [])
        for relative, content in ((RULE_PATH, "  \n"), (CASE_DIRECTORY + "/blank.md", "\n\t"),
                                  (CASE_DIRECTORY + "/unknown.txt", "must not be silently ignored")):
            with self.subTest(relative=relative):
                self.set_mode("normal")
                self.write(relative, content)
                kind = "rules" if relative == RULE_PATH else "fixtures"
                self.assert_status(self.check_quality(kind), kind, "incomplete")
                self.assertEqual(self.calls(), [])
                (self.repo / relative).unlink()
        self.assert_status(self.check_quality("rules"), "rules", "incomplete")
        self.assertEqual(self.calls(), [])
        shutil.rmtree(cases)
        self.assert_status(self.check_quality("fixtures"), "fixtures", "incomplete")
        self.assertEqual(self.calls(), [])

    def test_material_engine_and_config_changes_during_a_call_invalidate_the_result(self):
        targets = [self.repo / RULE_PATH, self.engine / "principles.md", self.engine / "README.md",
                   self.engine / "prompts/material-check.md", self.engine / "runner/material_check.py",
                   self.engine / "runner/codex_model.py", self.config_path]
        for target in targets:
            with self.subTest(target=target.relative_to(self.root).as_posix()):
                original = target.read_bytes()
                self.set_mode("change_during_call", change_target=str(target))
                try:
                    self.assert_status(self.check_quality("rules"), "rules", "incomplete")
                finally:
                    target.write_bytes(original)
        fixture_path = self.repo / next(iter(self.fixtures))
        self.set_mode("change_during_call", change_target=str(fixture_path))
        self.assert_status(self.check_quality("fixtures"), "fixtures", "incomplete")

    def test_missing_or_empty_engine_instructions_do_not_fall_back_to_product_rules(self):
        for name in ("principles.md", "README.md", "prompts/material-check.md"):
            path = self.engine / name
            original = path.read_bytes()
            for content in (None, b" \n"):
                with self.subTest(name=name, content=content):
                    self.set_mode("normal")
                    if content is None:
                        path.unlink()
                    else:
                        path.write_bytes(content)
                    try:
                        self.assert_status(self.check_quality("rules"), "rules", "incomplete")
                        self.assertEqual(self.calls(), [])
                    finally:
                        path.write_bytes(original)

    def test_new_input_engine_or_config_versions_receive_new_snapshot_identities(self):
        pointer, _ = self.assert_status(self.check_quality("rules"), "rules", "passed")
        previous = pointer["snapshot_id"]
        for target, suffix in ((self.repo / RULE_PATH, "\nNEW_RULE_VERSION\n"),
                               (self.engine / "principles.md", "\nNEW_PRINCIPLES_VERSION\n"),
                               (self.engine / "prompts/material-check.md", "\nNEW_PROMPT_VERSION\n")):
            with self.subTest(target=target.name):
                target.write_text(target.read_text() + suffix)
                self.set_mode("normal")
                pointer, _ = self.assert_status(self.check_quality("rules"), "rules", "passed")
                self.assertNotEqual(pointer["snapshot_id"], previous)
                previous = pointer["snapshot_id"]
        configuration = json.loads(self.config_path.read_text())
        configuration["model"] = "another-offline-model"
        self.config_path.write_text(json.dumps(configuration))
        self.set_mode("normal")
        pointer, _ = self.assert_status(self.check_quality("rules"), "rules", "passed")
        self.assertNotEqual(pointer["snapshot_id"], previous)

    def test_relative_repo_from_nested_directory_uses_git_root_and_paths_with_spaces(self):
        nested = self.repo / "nested" / "working directory"
        nested.mkdir(parents=True)
        for kind in ("rules", "fixtures"):
            with self.subTest(kind=kind):
                self.set_mode("normal")
                self.assert_status(self.check_quality(kind, cwd=nested, repo="."), kind, "passed")
                self.assertFalse((nested / ".clarity").exists())
                self.assertFalse((nested / ".git").exists())

    def test_invalid_repository_is_incomplete_without_creating_review_state(self):
        outside = self.root / "not a repository"
        outside.mkdir()
        for kind in ("rules", "fixtures"):
            with self.subTest(kind=kind):
                result = self.check_quality(kind, repo=outside)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
