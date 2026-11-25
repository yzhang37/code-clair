"""test_versioning_flow.py 通过真实安装入口验证版本选择、查询与资料保留。

所有来源、标签、宿主和 Git 配置均在临时目录；这些检查不调用模型或执行业务代码。
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


FACTORY = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = "https://github.com/yzhang37/code-clair.git"
STAMP = "2026-11-25T12:00:00Z"


class VersioningFlowTests(unittest.TestCase):
    """VersioningFlowTests() 用真实 Git 标签和命令区分发布版本、开发源码及安装记录。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-version-flow-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "factory"
        shutil.copytree(FACTORY, self.source,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".DS_Store", "*.pyc"))
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("GIT_")
                            and key not in {"OPENAI_API_KEY", "CODEX_API_KEY"}}
        self.environment.update(CLARITY_LANG="en", GIT_CONFIG_NOSYSTEM="1",
                                GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                                PYTHONDONTWRITEBYTECODE="1")
        self.host = self.root / "plain host"
        self.host.mkdir()
        self.initialize(self.source)
        (self.source / "engine/release.json").unlink(missing_ok=True)
        self.legacy_commit = self.commit("historical unversioned source")
        self.first_commit = self.publish("1.0.0")

    def command(self, arguments, *, cwd=None, check=True, environment=None):
        """command() 为每条命令保留终端证据；失败分支由测试显式检查退出码和安装原文。"""
        completed = subprocess.run(arguments, cwd=cwd or self.host,
                                   env=self.environment if environment is None else environment,
                                   text=True, capture_output=True, timeout=40)
        if check:
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed

    def git(self, directory, *arguments):
        return self.command(["git", "-C", str(directory), *arguments], cwd=directory).stdout.strip()

    def initialize(self, directory):
        self.git(directory, "init", "-q", "-b", "main")
        self.git(directory, "config", "user.name", "Version Flow Fixture")
        self.git(directory, "config", "user.email", "version-flow@example.invalid")
        self.git(directory, "config", "commit.gpgsign", "false")

    def commit(self, message):
        self.git(self.source, "add", "--all")
        self.git(self.source, "commit", "-q", "-m", message)
        return self.git(self.source, "rev-parse", "HEAD")

    def publish(self, version, *, built_at=STAMP, tag=True, marker=None, annotated=False):
        """publish() 在提交中固定版本资料，再将发布标签绑定实际提交；工作树改动不会代替发布。"""
        metadata = self.source / "engine/release.json"
        metadata.write_text(json.dumps({"product_version": version, "built_at": built_at}) + "\n")
        if marker is not None:
            (self.source / "engine/version-test-marker.txt").write_text(marker + "\n")
        commit = self.commit("fixture release " + version)
        if tag:
            arguments = ["tag", "v" + version]
            if annotated:
                arguments = ["tag", "-a", "v" + version, "-m", "Version fixture " + version]
            self.git(self.source, *arguments)
        return commit

    def install(self, *, ref=None, source=True, track="no", check=True):
        arguments = [sys.executable, str(self.source / "setup.py"), "install",
                     "--repo", str(self.host), "--track", track]
        if source:
            arguments.extend(["--source", str(self.source)])
        if ref is not None:
            arguments.extend(["--ref", ref])
        return self.command(arguments, check=check)

    def update(self, *arguments, check=True, environment=None):
        return self.command([str(self.host / ".clarity/update"), *arguments], check=check,
                            environment=environment)

    def state(self):
        local = self.host / ".clarity/.local"
        if (local / "installation.json").is_file():
            return local
        root = self.git(self.host, "rev-parse", "--show-toplevel")
        common = Path(self.git(Path(root), "rev-parse", "--git-common-dir"))
        return (common if common.is_absolute() else Path(root) / common).resolve() / "clarity"

    def manifest(self):
        return json.loads((self.state() / "installation.json").read_text())

    def current(self):
        return self.tree(self.host / ".clarity/current")

    def tree(self, directory, *, omit_download_cache=False):
        """tree() 比较目录、正文及权限；检查更新只排除允许刷新的来源下载缓存。"""
        directory = Path(directory)
        result = {}
        if not directory.exists():
            return result
        for path in directory.rglob("*"):
            relative = path.relative_to(directory)
            cache_paths = (".clarity/.local/source", ".clarity/.local/sources",
                           ".git/clarity/source", ".git/clarity/sources")
            if omit_download_cache and any(relative == Path(prefix) or Path(prefix) in relative.parents
                                           for prefix in cache_paths):
                continue
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                value = ("link", mode, os.readlink(path))
            elif path.is_dir():
                value = ("directory", mode)
            else:
                value = ("file", mode, path.read_bytes())
            result[relative.as_posix()] = value
        return result

    def owner_configuration(self):
        """owner_configuration() 保存已有规则采用原文和偏好，更新不能借版本识别重新采用资料。"""
        rules = self.host / ".clarity/current/rules.md"
        raw = rules.read_bytes()
        name = ".clarity/current/rules.md"
        config = {"agent": "custom", "agent_command": ["fixture-model-must-not-run"],
                  "model": "owner-model", "reviewers": 3, "timeout": 43,
                  "owner_preference": {"preserve": ["these", "settings"]},
                  "basis": [name], "basis_source": "local", "basis_selections": [name],
                  "basis_worktree": str(self.host), "basis_ignored": {},
                  "basis_hashes": {name: hashlib.sha256(raw).hexdigest()},
                  "basis_snapshot": {name: {"text": raw.decode(),
                                             "oid": "sha256:" + hashlib.sha256(raw).hexdigest(),
                                             "mode": "100644"}}}
        (self.state() / "config.json").write_text(json.dumps(config) + "\n")
        return config

    def assert_configuration_preserved(self, expected):
        config = json.loads((self.state() / "config.json").read_text())
        self.assertEqual({key: value for key, value in config.items() if key != "engine_digest"}, expected)

    def test_fresh_install_uses_numeric_stable_order_and_exact_committed_seed(self):
        self.publish("4.2.0", built_at="2026-11-25T20:00:00Z")
        (self.source / "current/release-marker.txt").write_text("committed stable seed\n")
        latest = self.publish("4.10.0", built_at="2026-11-24T20:00:00Z", annotated=True)
        self.publish("5.0.0-rc.1")
        (self.source / "current/release-marker.txt").write_text("uncommitted seed must not deploy\n")
        self.install()
        self.assertEqual(self.manifest()["commit"], latest)
        metadata = json.loads((self.host / ".clarity/engine/release.json").read_text())
        self.assertEqual(metadata, {"product_version": "4.10.0", "built_at": "2026-11-24T20:00:00Z"})
        self.assertEqual((self.host / ".clarity/current/release-marker.txt").read_text(),
                         "committed stable seed\n")
        self.assertFalse((self.host / ".git").exists())
        self.assertTrue((self.host / "Makefile").is_file())

    def test_offline_version_needs_no_git_and_writes_nothing(self):
        self.install()
        before = self.tree(self.host)
        binary = self.root / "offline-bin"
        binary.mkdir()
        for name in ("sh", "dirname"):
            (binary / name).symlink_to(shutil.which(name))
        (binary / "python3").symlink_to(sys.executable)
        environment = dict(self.environment, PATH=str(binary),
                           CLARITY_PYTHON=str(self.root / "missing-sdk-python"))
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        self.assertIsNone(shutil.which("git", path=environment["PATH"]))
        self.source.rename(self.root / "offline-source")
        result = self.update("--version", environment=environment)
        self.assertIn("1.0.0", result.stdout)
        self.assertIn(STAMP, result.stdout)
        self.assertIn(self.first_commit, result.stdout)
        self.assertEqual(self.tree(self.host), before)

    def test_check_on_mature_git_preserves_deployment_preferences_hooks_and_business_index(self):
        self.initialize(self.host)
        code = self.host / "business.py"
        code.write_text("value = 1\n")
        self.git(self.host, "add", "business.py")
        self.git(self.host, "commit", "-q", "-m", "Mature project")
        code.write_text("value = 2\n")
        self.git(self.host, "add", "business.py")
        code.write_text("value = 3\n")
        self.install()
        self.owner_configuration()
        hook = self.host / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        self.publish("1.1.0")
        before = self.tree(self.host, omit_download_cache=True)
        result = self.update("--check")
        self.assertIn("1.0.0", result.stdout)
        self.assertIn("1.1.0", result.stdout)
        self.assertEqual(self.tree(self.host, omit_download_cache=True), before)

    def test_stable_updates_continue_tracking_new_releases_and_preserve_adoption(self):
        self.install()
        owner = self.host / ".clarity/current/owner.txt"
        owner.write_bytes(b"owner material\r\n")
        expected_current = self.current()
        config = self.owner_configuration()
        for version in ("1.1.0", "1.2.0"):
            expected_commit = self.publish(version)
            self.update()
            self.assertEqual(self.manifest()["commit"], expected_commit)
            self.assertEqual(self.current(), expected_current)
            self.assert_configuration_preserved(config)
        result = self.update("--version")
        self.assertIn("1.2.0", result.stdout)

    def test_later_timestamp_on_an_older_head_is_not_an_automatic_downgrade(self):
        newest = self.publish("2.0.0", built_at="2026-11-24T12:00:00Z")
        self.install(ref="HEAD")
        self.publish("1.9.0", built_at="2026-11-25T23:00:00Z")
        before = self.tree(self.host, omit_download_cache=True)
        checked = self.update("--check")
        self.assertIn("2.0.0", checked.stdout)
        self.assertIn("1.9.0", checked.stdout)
        refused = self.update(check=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(self.manifest()["commit"], newest)
        self.assertEqual(self.tree(self.host, omit_download_cache=True), before)

    def test_explicit_old_tag_downgrade_preserves_current_and_pins_that_selection(self):
        self.install()
        expected_current = self.current()
        config = self.owner_configuration()
        self.publish("1.1.0")
        self.update()
        self.update("--ref", "v1.0.0")
        self.assertEqual(self.manifest()["commit"], self.first_commit)
        self.assertEqual(self.current(), expected_current)
        self.assert_configuration_preserved(config)
        self.publish("1.2.0")
        self.update()
        self.assertEqual(self.manifest()["commit"], self.first_commit)

    def test_rebinding_a_formal_version_is_an_identity_conflict(self):
        self.install()
        self.publish("1.0.0", tag=False, marker="different factory program contents")
        self.git(self.source, "tag", "-f", "v1.0.0")
        before = self.tree(self.host, omit_download_cache=True)
        for arguments in (("--check",), ()):
            with self.subTest(arguments=arguments):
                result = self.update(*arguments, check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("conflict", (result.stdout + result.stderr).lower())
                self.assertEqual(self.tree(self.host, omit_download_cache=True), before)

    def test_explicit_development_branch_is_not_misreported_as_a_formal_release(self):
        self.install()
        first_development = self.publish("1.1.0", tag=False)
        self.update("--check")
        self.assertEqual(self.manifest()["commit"], self.first_commit)
        self.update("--ref", "main")
        self.assertEqual(self.manifest()["commit"], first_development)
        description = self.update("--version").stdout.lower()
        self.assertIn("development", description)
        next_development = self.publish("1.2.0", tag=False)
        self.update()
        self.assertEqual(self.manifest()["commit"], next_development)

    def test_legacy_schema_keeps_unknown_identity_then_migrates_without_resetting_preferences(self):
        self.install(ref=self.legacy_commit)
        state = self.state()
        path = state / "installation.json"
        record = json.loads(path.read_text())
        old_keys = {"version", "worktree_root", "source", "ref", "commit", "previous_commit",
                    "previous_source", "installed_at", "host_makefile", "track_git", "files", "hooks"}
        record = {key: value for key, value in record.items() if key in old_keys}
        record["ref"] = "HEAD"
        path.write_text(json.dumps(record) + "\n")
        self.assertFalse((self.host / ".clarity/engine/release.json").exists())
        config = self.owner_configuration()
        expected_current = self.current()
        description = self.update("--version").stdout.lower()
        self.assertIn("unversioned", description)
        self.assertIn(self.legacy_commit, description)
        self.assertNotIn("0.0.0", description)
        self.update()
        self.assertEqual(self.manifest()["version"], 1)
        self.assertEqual(self.manifest()["commit"], self.first_commit)
        self.assertEqual(self.manifest()["previous_commit"], self.legacy_commit)
        self.assertEqual(self.current(), expected_current)
        self.assert_configuration_preserved(config)
        self.assertIn("1.0.0", self.update("--version").stdout)

    def test_same_release_update_repairs_programs_and_restores_only_a_whole_missing_current(self):
        self.install()
        config = self.owner_configuration()
        expected_current = self.current()
        program = self.host / ".clarity/engine/runner/review.py"
        expected_program = program.read_bytes()
        program.unlink()
        (self.host / ".clarity/initial/rules.md").unlink()
        self.update()
        self.assertEqual(program.read_bytes(), expected_program)
        self.assertEqual(self.current(), expected_current)
        self.assert_configuration_preserved(config)
        shutil.rmtree(self.host / ".clarity/current")
        self.update()
        self.assertEqual(self.current(), self.tree(self.host / ".clarity/initial"))
        new_config = json.loads((self.state() / "config.json").read_text())
        self.assertTrue(new_config["basis_reset"])
        self.assertNotIn("basis_snapshot", new_config)
        for key in ("agent", "agent_command", "model", "timeout", "reviewers", "owner_preference"):
            self.assertEqual(new_config[key], config[key])

    def test_nested_unicode_install_and_version_lookup_stay_at_the_selected_directory(self):
        parent = self.host
        self.initialize(parent)
        business = parent / "parent.py"
        business.write_text("PARENT = True\n")
        self.git(parent, "add", "parent.py")
        self.git(parent, "commit", "-q", "-m", "Parent project")
        parent_index = (parent / ".git/index").read_bytes()
        self.host = parent / "有 空格的子目录"
        self.host.mkdir()
        self.install()
        self.assertFalse((parent / ".clarity").exists())
        self.assertFalse((self.host / ".git").exists())
        before = self.tree(self.host)
        result = self.update("--version")
        self.assertIn("1.0.0", result.stdout)
        self.assertEqual(self.tree(self.host), before)
        self.assertEqual((parent / ".git/index").read_bytes(), parent_index)
        self.assertEqual(business.read_text(), "PARENT = True\n")

    def test_failed_update_check_preserves_installed_state_and_does_not_claim_latest(self):
        self.install()
        before = self.tree(self.host, omit_download_cache=True)
        self.source.rename(self.root / "unavailable-source")
        result = self.update("--check", check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("already up to date", (result.stdout + result.stderr).lower())
        self.assertEqual(self.tree(self.host, omit_download_cache=True), before)

    def test_default_https_and_explicit_local_source_install_the_same_release_identity(self):
        self.install()
        local_metadata = (self.host / ".clarity/engine/release.json").read_bytes()
        local_commit = self.manifest()["commit"]
        rewrite = self.root / "private-git-config"
        self.command(["git", "config", "--file", str(rewrite),
                      "url." + self.source.as_uri() + ".insteadOf", DEFAULT_SOURCE])
        self.environment["GIT_CONFIG_GLOBAL"] = str(rewrite)
        self.host = self.root / "https host"
        self.host.mkdir()
        self.install(source=False)
        self.assertEqual(self.manifest()["source"], DEFAULT_SOURCE)
        self.assertEqual(self.manifest()["commit"], local_commit)
        self.assertEqual((self.host / ".clarity/engine/release.json").read_bytes(), local_metadata)

    def test_invalid_version_fields_cannot_be_deployed_even_with_an_explicit_branch(self):
        self.install()
        before = self.tree(self.host, omit_download_cache=True)
        for version in ("1.02.0", "01.2.0", "1.2.03", "1.2.3-rc.01", "4.2"):
            with self.subTest(version=version):
                self.publish(version, tag=False)
                result = self.update("--ref", "main", check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertEqual(self.tree(self.host, omit_download_cache=True), before)


if __name__ == "__main__":
    unittest.main()
