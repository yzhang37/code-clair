"""test_bootstrap_versions.py 用真实 Git 对象验证引导层只执行已选正式发布的管理程序。

HTTPS 地址在每个测试的私有 Git 配置中映射到本地来源；不调用模型、不安装真实项目。
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


FACTORY = Path(__file__).resolve().parents[1]
SOURCE = "https://github.com/yzhang37/code-clair.git"
MANAGER = '''import importlib.util,json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("bootstrap_fixture_helper",root/"engine/runner/release.py")
helper=importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
record={"manager":__MARKER__,"helper":helper.MARKER,
        "locale":json.loads((root/"engine/runner/locales/aliases.json").read_text())["marker"],
        "arguments":sys.argv[1:],"target":str(Path.cwd())}
Path(os.environ["CLARITY_BOOTSTRAP_RESULT"]).write_text(json.dumps(record))
'''


class BootstrapVersionTests(unittest.TestCase):
    """BootstrapVersionTests() 将来源、标签、管理器执行证据和宿主隔离在临时目录中。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-bootstrap-version-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "factory"
        self.source.mkdir()
        self.host = self.root / "新 project"
        self.host.mkdir()
        self.result = self.root / "manager-result.json"
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(self.root / "git-config"),
                                GIT_TERMINAL_PROMPT="0", CLARITY_BOOTSTRAP_RESULT=str(self.result),
                                PYTHONDONTWRITEBYTECODE="1")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Bootstrap Version Fixture")
        self.git("config", "user.email", "bootstrap-version@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.execute(["git", "config", "--file", self.environment["GIT_CONFIG_GLOBAL"],
                      "url." + self.source.as_uri() + ".insteadOf", SOURCE])

    def execute(self, command, *, cwd=None, check=True):
        """execute() 为真实子进程保留证据，并让拒绝分支由用例核对而不丢弃终端输出。"""
        completed = subprocess.run(command, cwd=cwd or self.source, env=self.environment,
                                   text=True, capture_output=True, timeout=30)
        if check:
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed

    def git(self, *arguments):
        return self.execute(["git", *arguments]).stdout.strip()

    def publish(self, version, *, marker=None, tag=None, annotated=False, stamp="2026-11-25T12:00:00Z"):
        """publish() 先提交同版启动文件与声明，再打标签；之后的 HEAD 不替换该发布对象。"""
        marker = marker or version
        runner = self.source / "engine/runner"
        (runner / "locales").mkdir(parents=True, exist_ok=True)
        (runner / "release.py").write_text("MARKER = " + repr(marker) + "\n")
        (runner / "i18n.py").write_text("MARKER = " + repr(marker) + "\n")
        (runner / "locales/aliases.json").write_text(json.dumps({"marker": marker}) + "\n")
        (self.source / "engine/release.json").write_text(json.dumps({"product_version": version, "built_at": stamp}) + "\n")
        manager = MANAGER.replace("__MARKER__", repr(marker))
        (self.source / "setup.py").write_text(manager)
        self.git("add", "--all")
        self.git("commit", "-q", "-m", "bootstrap fixture " + marker)
        commit = self.git("rev-parse", "HEAD")
        if tag is not False:
            name = tag or "v" + version
            self.git(*(["tag", "-a", name, "-m", "fixture " + name] if annotated else ["tag", name]))
        return commit

    def launch(self, *arguments):
        return self.execute(["sh", str(FACTORY / "install.sh"), *arguments], cwd=self.host, check=False)

    def assert_not_executed(self, completed):
        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertFalse(self.result.exists())
        self.assertEqual(list(self.host.iterdir()), [])
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)

    def test_numeric_stable_tag_selects_same_commit_dependencies_and_forwards_host_arguments(self):
        self.publish("4.2.0")
        self.publish("4.10.0", annotated=True)
        self.publish("99.0.0-rc.1", marker="preview must not execute")
        self.publish("88.00.0", marker="leading zero must not execute")
        self.publish("100.0.0", marker="unpublished HEAD must not execute", tag=False)
        completed = self.launch("--source", "/custom source", "--ref", "HEAD")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        record = json.loads(self.result.read_text())
        self.assertEqual((record["manager"], record["helper"], record["locale"]), ("4.10.0",) * 3)
        self.assertEqual(record["arguments"], ["install", "--source", "/custom source", "--ref", "HEAD", "--repo", str(self.host)])
        self.assertEqual(record["target"], str(self.host))
        self.assertEqual(list(self.host.iterdir()), [])

    def test_source_with_only_preview_tags_does_not_execute_head(self):
        self.publish("1.0.0-rc.1")
        completed = self.launch()
        self.assert_not_executed(completed)
        self.assertIn("No stable Clarity release", completed.stderr)

    def test_highest_tag_metadata_mismatch_does_not_fall_back_to_older_release(self):
        self.publish("1.0.0")
        self.publish("1.1.0", tag="v2.0.0")
        completed = self.launch()
        self.assert_not_executed(completed)
        self.assertIn("does not match", completed.stderr)

    def test_equal_precedence_build_tags_are_not_ordered_by_metadata_or_hash(self):
        self.publish("1.0.0+first")
        self.publish("1.0.0+second")
        completed = self.launch()
        self.assert_not_executed(completed)
        self.assertIn("same version precedence", completed.stderr)

    def test_malformed_build_timestamp_does_not_execute_manager(self):
        self.publish("1.0.0", stamp="2026-11-25T12:00:00")
        completed = self.launch()
        self.assert_not_executed(completed)
        self.assertIn("UTC timestamp ending in Z", completed.stderr)


if __name__ == "__main__":
    unittest.main()
