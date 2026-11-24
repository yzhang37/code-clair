#!/usr/bin/env python3
"""test_setup.py() 用本地 Git 和临时宿主核对软件安装与资料保留边界。

测试只运行安装管理命令；即使准备已有审核配置，也只使用不会被调用的本地占位程序。
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


FACTORY = Path(__file__).resolve().parents[1]


class SidecarSetupTests(unittest.TestCase):
    """SidecarSetupTests() 使每项安装实验拥有独立原厂历史和宿主，不接触真实项目。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-setup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "factory"
        shutil.copytree(FACTORY, self.source,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".DS_Store", "*.pyc"))
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("GIT_")
                            and key not in {"CODEX_API_KEY", "OPENAI_API_KEY"}}
        self.environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                 "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1",
                                 "CODEX_HOME": str(self.root / "unused-codex-home")})
        self.initialize(self.source)
        self.commit("initial factory")
        self.host = self.new_host("host")

    def run_command(self, command, *, cwd=None, check=True):
        """run_command() 保留子进程诊断，并为失败场景允许显式检查非零结果。"""
        result = subprocess.run(command, cwd=cwd or self.host, env=self.environment,
                                text=True, capture_output=True, timeout=30)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, repo, *arguments):
        return self.run_command(["git", "-C", str(repo), *arguments], cwd=repo).stdout.strip()

    def initialize(self, repo):
        self.git(repo, "init", "-q", "-b", "main")
        self.git(repo, "config", "user.name", "Offline Setup Fixture")
        self.git(repo, "config", "user.email", "setup-fixture@example.invalid")
        self.git(repo, "config", "commit.gpgsign", "false")

    def new_host(self, name):
        repo = self.root / name
        repo.mkdir()
        self.initialize(repo)
        return repo

    def commit(self, message):
        self.git(self.source, "add", "--all")
        self.git(self.source, "commit", "-q", "-m", message)
        return self.git(self.source, "rev-parse", "HEAD")

    def install(self, *, host=None, check=True):
        target = host or self.host
        return self.run_command([sys.executable, str(self.source / "setup.py"), "install",
                                 "--repo", str(target), "--source", str(self.source), "--track", "yes"],
                                cwd=target, check=check)

    def update(self, *, check=True):
        return self.run_command([sys.executable, str(self.host / ".clarity/setup.py"), "update",
                                 "--repo", str(self.host)], check=check)

    def manifest(self):
        return json.loads((self.host / ".git/clarity/installation.json").read_text())

    def installed_bytes(self):
        """installed_bytes() 比较用户可运行的部署及其记录，不把可刷新的 Git 下载缓存算入部署。"""
        files = {str(path.relative_to(self.host)): path.read_bytes()
                 for path in (self.host / ".clarity").rglob("*") if path.is_file()}
        for relative in ("Makefile", ".git/clarity/installation.json", ".git/clarity/config.json"):
            path = self.host / relative
            if path.is_file():
                files[relative] = path.read_bytes()
        return files

    def test_first_install_uses_factory_templates_and_records_git_origin(self):
        self.install()
        sidecar = self.host / ".clarity"
        self.assertTrue((sidecar / "setup.py").is_file())
        self.assertTrue((sidecar / "engine/runner/review.py").is_file())
        self.assertEqual((sidecar / "current/rules.md").read_bytes(),
                         (self.source / "initial/rules.md").read_bytes())
        self.assertEqual((sidecar / "current/cases/case.md").read_bytes(),
                         (self.source / "initial/cases/case.md").read_bytes())
        self.assertFalse((sidecar / "current/cases/0001. multilingual-todo.md").exists())
        self.assertIn(b".clarity/engine/runner/review.py", (sidecar / "Makefile").read_bytes())
        self.assertIn(b"include .clarity/Makefile", (self.host / "Makefile").read_bytes())
        installation = self.manifest()
        self.assertEqual(installation["commit"], self.git(self.source, "rev-parse", "HEAD"))
        self.assertIn(str(self.source), json.dumps(installation, ensure_ascii=False))
        self.assertTrue(installation["files"])
        self.assertFalse((self.host / ".git/clarity/config.json").exists())
        self.assertFalse((self.host / ".git/hooks/commit-msg").exists())
        self.assertFalse((self.host / ".git/hooks/pre-commit").exists())

    def test_reinstall_preserves_current_and_two_hosts_keep_separate_materials(self):
        self.install()
        current = self.host / ".clarity/current/rules.md"
        current.write_bytes(b"First host's own rules.\n")
        self.install()
        self.assertEqual(current.read_bytes(), b"First host's own rules.\n")
        self.assertEqual((self.host / "Makefile").read_bytes().count(b"include .clarity/Makefile"), 1)
        second = self.new_host("second-host")
        self.install(host=second)
        self.assertEqual((second / ".clarity/current/rules.md").read_bytes(),
                         (self.source / "initial/rules.md").read_bytes())
        self.assertEqual(current.read_bytes(), b"First host's own rules.\n")

    def test_update_adds_and_removes_factory_files_without_changing_current(self):
        obsolete = self.source / "engine/obsolete.txt"
        obsolete.write_bytes(b"old factory file\n")
        self.commit("factory with obsolete file")
        self.install()
        current = self.host / ".clarity/current/rules.md"
        current.write_bytes(b"Host-owned rules survive every update.\n")
        obsolete.unlink()
        (self.source / "engine/new-file.txt").write_bytes(b"new factory file\n")
        (self.source / "initial/rules.md").write_bytes(b"New factory template.\n")
        latest = self.commit("new factory version")
        self.update()
        self.assertFalse((self.host / ".clarity/engine/obsolete.txt").exists())
        self.assertEqual((self.host / ".clarity/engine/new-file.txt").read_bytes(), b"new factory file\n")
        self.assertEqual((self.host / ".clarity/initial/rules.md").read_bytes(), b"New factory template.\n")
        self.assertEqual(current.read_bytes(), b"Host-owned rules survive every update.\n")
        self.assertEqual(self.manifest()["commit"], latest)

    def test_update_rejects_locally_edited_factory_files_without_partial_deployment(self):
        self.install()
        local = self.host / ".clarity/engine/workflow.md"
        local.write_bytes(local.read_bytes() + b"\nLocal edit must be preserved.\n")
        (self.source / "engine/new-file.txt").write_bytes(b"new release\n")
        self.commit("upstream change")
        before = self.installed_bytes()
        result = self.update(check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.installed_bytes(), before)

    def test_fetch_failure_leaves_the_previous_installation_usable(self):
        self.install()
        before = self.installed_bytes()
        self.source.rename(self.root / "offline-source")
        result = self.update(check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.installed_bytes(), before)
        result = self.run_command(["make", "-n", "review"], check=True)
        self.assertIn(".clarity/engine/runner/review.py", result.stdout)

    def test_host_makefile_keeps_user_content_and_default_target(self):
        original = b"CUSTOM = host-value\n\nbuild:\n\t@echo host-build\n"
        (self.host / "Makefile").write_bytes(original)
        self.install()
        self.assertTrue((self.host / "Makefile").read_bytes().startswith(original))
        self.assertEqual(self.run_command(["make", "-n"]).stdout.strip(), "echo host-build")
        self.install()
        self.assertTrue((self.host / "Makefile").read_bytes().startswith(original))
        self.assertEqual((self.host / "Makefile").read_bytes().count(b"include .clarity/Makefile"), 1)

    def test_conflicting_host_review_target_is_preserved_without_installing_sidecar(self):
        original = b"review:\n\t@echo host-review\n"
        (self.host / "Makefile").write_bytes(original)
        result = self.install(check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.host / "Makefile").read_bytes(), original)
        self.assertFalse((self.host / ".clarity").exists())
        self.assertFalse((self.host / ".git/clarity/installation.json").exists())

    def test_update_refreshes_existing_runtime_without_readopting_rules_or_resetting_settings(self):
        self.git(self.host, "config", "core.hooksPath", ".custom-hooks")
        self.install()
        rule = self.host / ".clarity/current/rules.md"
        rule.write_bytes(b"Guard empty sequences before indexing their first element.\n")
        self.run_command([sys.executable, str(self.host / ".clarity/engine/runner/install.py"),
                          "--repo", str(self.host), "--basis", ".clarity/current/rules.md",
                          "--codex-bin", sys.executable, "--model", "offline-custom-model",
                          "--reviewers", "3", "--timeout", "37"])
        config_path = self.host / ".git/clarity/config.json"
        config = json.loads(config_path.read_text())
        config["future_setting"] = {"keep": True}
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        hook = self.host / ".custom-hooks/pre-commit"
        original_hook = hook.read_bytes()
        self.assertFalse((self.host / ".git/hooks/pre-commit").exists())
        rule.write_bytes(b"Changed host rules must remain candidates after updating software.\n")
        workflow = self.source / "engine/workflow.md"
        workflow.write_bytes(workflow.read_bytes() + b"\nOffline factory version marker.\n")
        self.commit("runtime engine update")
        self.update()
        actual = json.loads(config_path.read_text())
        self.assertNotEqual(actual["engine_digest"], config["engine_digest"])
        self.assertEqual({key: value for key, value in actual.items() if key != "engine_digest"},
                         {key: value for key, value in config.items() if key != "engine_digest"})
        self.assertEqual((self.host / ".git/clarity/engine/workflow.md").read_bytes(), workflow.read_bytes())
        self.assertEqual(hook.read_bytes(), original_hook)
        self.assertFalse((self.host / ".git/hooks/pre-commit").exists())
        self.assertEqual(rule.read_bytes(), b"Changed host rules must remain candidates after updating software.\n")

    def test_legacy_sidecar_is_adopted_from_matching_history_without_resetting_project_state(self):
        specification = importlib.util.spec_from_file_location(
            "clarity_legacy_make_fixture", self.source / "engine/runner/make_integration.py")
        make = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(make)
        legacy_makefile = make.rendered_sidecar_makefile()
        new_files = {}
        for name in ("setup.py", "engine/runner/make_integration.py", "engine/runner/test_make_integration.py"):
            path = self.source / name
            if path.exists():
                new_files[name] = path.read_bytes()
                path.unlink()
        (self.source / "Makefile").write_bytes(legacy_makefile)
        self.commit("legacy factory layout")
        sidecar = self.host / ".clarity"
        sidecar.mkdir()
        for directory in ("engine", "initial"):
            shutil.copytree(self.source / directory, sidecar / directory)
        shutil.copy2(self.source / "README.md", sidecar / "README.md")
        shutil.copytree(sidecar / "initial", sidecar / "current")
        (sidecar / "current/rules.md").write_bytes(b"Legacy host's adopted rules.\n")
        (self.host / "Makefile").write_bytes(legacy_makefile)
        self.run_command([sys.executable, str(sidecar / "engine/runner/install.py"),
                          "--repo", str(self.host), "--basis", ".clarity/current/rules.md",
                          "--codex-bin", sys.executable, "--model", "legacy-custom-model",
                          "--reviewers", "3", "--timeout", "41"])
        hooks = self.host / ".git/hooks"
        shutil.copy2(sidecar / "engine/runner/commit-msg", hooks / "commit-msg")
        (hooks / "pre-commit").unlink()
        config_path = self.host / ".git/clarity/config.json"
        previous_config = json.loads(config_path.read_text())
        previous_config["custom_legacy_setting"] = ["keep", "as-is"]
        config_path.write_text(json.dumps(previous_config) + "\n")
        previous_current = {str(path.relative_to(sidecar / "current")): path.read_bytes()
                            for path in (sidecar / "current").rglob("*") if path.is_file()}
        for name, content in new_files.items():
            (self.source / name).write_bytes(content)
        workflow = self.source / "engine/workflow.md"
        workflow.write_bytes(workflow.read_bytes() + b"\nNew version after legacy installation.\n")
        latest = self.commit("independent installer release")
        self.install()
        actual_config = json.loads(config_path.read_text())
        self.assertNotEqual(actual_config["engine_digest"], previous_config["engine_digest"])
        self.assertEqual({key: value for key, value in actual_config.items() if key != "engine_digest"},
                         {key: value for key, value in previous_config.items() if key != "engine_digest"})
        self.assertEqual({str(path.relative_to(sidecar / "current")): path.read_bytes()
                          for path in (sidecar / "current").rglob("*") if path.is_file()}, previous_current)
        self.assertEqual((self.host / ".git/clarity/engine/workflow.md").read_bytes(), workflow.read_bytes())
        self.assertEqual((hooks / "commit-msg").read_bytes(),
                         (sidecar / "engine/runner/commit-msg").read_bytes())
        self.assertFalse((hooks / "pre-commit").exists())
        self.assertTrue((sidecar / "setup.py").exists())
        self.assertEqual(self.manifest()["commit"], latest)
        self.assertIn(b"include .clarity/Makefile", (self.host / "Makefile").read_bytes())

    def test_partial_write_failure_restores_files_records_and_new_directories(self):
        self.install()
        specification = importlib.util.spec_from_file_location("clarity_setup_under_test", FACTORY / "setup.py")
        setup = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(setup)
        state = self.host / ".git/clarity"
        before = self.installed_bytes()
        new_directory = self.host / ".clarity/current/new-subdirectory"
        changes = {
            new_directory / "case.md": (b"must disappear after rollback\n", 0o644),
            self.host / ".clarity/engine/workflow.md": (b"replacement factory file\n", 0o644),
            self.host / "Makefile": (b"replacement host entry\n", 0o644),
            state / "installation.json": (b"replacement manifest\n", 0o600),
        }
        real_write = setup.write_file
        calls = 0

        def fail_second_write(path, value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected installation write failure")
            return real_write(path, value)

        with patch.object(setup, "write_file", side_effect=fail_second_write):
            with self.assertRaisesRegex(OSError, "injected installation write failure"):
                setup.apply_transaction(state, changes)
        self.assertGreater(calls, 2)
        self.assertEqual(self.installed_bytes(), before)
        self.assertFalse(new_directory.exists())
        self.assertEqual(list(state.glob("install-backup-*")), [])

    def test_sidecar_symlink_is_rejected_without_modifying_its_external_target(self):
        outside = self.root / "outside-materials"
        outside.mkdir()
        protected = outside / "important.txt"
        protected.write_bytes(b"unrelated external data\n")
        (self.host / ".clarity").symlink_to(outside, target_is_directory=True)
        result = self.install(check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.host / ".clarity").is_symlink())
        self.assertEqual(list(outside.iterdir()), [protected])
        self.assertEqual(protected.read_bytes(), b"unrelated external data\n")
        self.assertFalse((self.host / "Makefile").exists())
        self.assertFalse((self.host / ".git/clarity/installation.json").exists())


if __name__ == "__main__":
    unittest.main()
