"""test_install_flow.py 从用户所在目录核对检测、默认升级、Git 选择和非 Git 安装。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_setup as fixture


class InstallFlowTests(unittest.TestCase):
    """InstallFlowTests() 只使用一次性目录；业务暂存与工作树分别保留，避免把成功复制误作正确安装。"""

    run_command = fixture.SidecarSetupTests.run_command
    git = fixture.SidecarSetupTests.git
    initialize = fixture.SidecarSetupTests.initialize
    new_host = fixture.SidecarSetupTests.new_host
    commit = fixture.SidecarSetupTests.commit

    def setUp(self):
        fixture.SidecarSetupTests.setUp(self)

    def launch(self, target, answers="", *arguments):
        """launch() 省略 --repo，让测试实际使用进程当前目录，并保留提示及退出码供分支核对。"""
        result = subprocess.run([sys.executable, str(self.source / "setup.py"), "install",
                                 "--source", str(self.source), *arguments], cwd=target,
                                env=self.environment, input=answers, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def read_manifest(self, target):
        """read_manifest() 分别读取 Git 根目录的既有状态位置和独立目录的本机状态。"""
        path = target / ".git/clarity/installation.json"
        if not path.exists():
            path = target / ".clarity/.local/installation.json"
        return json.loads(path.read_text())

    def prepare_business_changes(self):
        """prepare_business_changes() 使同一业务文件同时有已暂存和未暂存内容，检验安装不会夹带它们。"""
        business = self.host / "business.txt"
        business.write_text("committed\n")
        (self.host / "Makefile").write_text("build:\n\t@echo existing-build\n")
        self.git(self.host, "add", "business.txt", "Makefile")
        self.git(self.host, "commit", "-q", "-m", "existing project")
        business.write_text("staged business edit\n")
        self.git(self.host, "add", "business.txt")
        business.write_text("unstaged business edit\n")
        makefile = self.host / "Makefile"
        makefile.write_text(makefile.read_text() + "\nSTAGED_BUSINESS = yes\n")
        self.git(self.host, "add", "Makefile")
        makefile.write_text(makefile.read_text() + "UNSTAGED_BUSINESS = yes\n")

    def test_no_git_installs_and_upgrades_without_creating_git_or_project_makefile(self):
        target = self.root / "not-a-git-project"
        target.mkdir()
        first = self.launch(target)
        self.assertNotIn("Git 跟踪", first.stdout)
        self.assertFalse((target / ".git").exists())
        self.assertFalse((target / "Makefile").exists())
        self.assertEqual({p.name for p in target.iterdir()}, {".clarity"})
        self.assertEqual((target / ".clarity/current/rules.md").read_bytes(),
                         (target / ".clarity/initial/rules.md").read_bytes())
        rule = target / ".clarity/current/rules.md"
        rule.write_text("synthetic local rules\n")
        self.run_command(["sh", str(target / ".clarity/update")], cwd=target)
        self.assertEqual(rule.read_text(), "synthetic local rules\n")
        self.assertFalse((target / ".git").exists())
        self.assertEqual(self.read_manifest(target)["worktree_root"], str(target))
        self.initialize(target)
        self.run_command(["sh", str(target / ".clarity/update")], cwd=target)
        self.assertEqual(self.read_manifest(target)["source"], str(self.source))
        self.assertEqual(rule.read_text(), "synthetic local rules\n")
        self.assertTrue((target / ".clarity/.local/installation.json").is_file())
        self.assertFalse((target / ".git/clarity/installation.json").exists())

    def test_existing_git_default_no_preserves_business_index_and_build(self):
        self.prepare_business_changes()
        index = (self.host / ".git/index").read_bytes()
        makefile = (self.host / "Makefile").read_bytes()
        result = self.launch(self.host, "\n")
        self.assertIn("[y/N]", result.stdout)
        self.assertEqual((self.host / ".git/index").read_bytes(), index)
        self.assertEqual((self.host / "Makefile").read_bytes(), makefile)
        self.assertEqual((self.host / "business.txt").read_text(), "unstaged business edit\n")
        self.assertEqual(self.git(self.host, "show", ":business.txt"), "staged business edit")
        self.assertEqual(self.git(self.host, "ls-files", "--", ".clarity"), "")
        self.assertEqual(self.git(self.host, "check-ignore", ".clarity/setup.py"), ".clarity/setup.py")
        self.assertFalse(self.read_manifest(self.host)["track_git"])

    def test_yes_stages_only_sidecar_without_business_or_local_cache(self):
        self.prepare_business_changes()
        self.launch(self.host, "y\n")
        staged = self.git(self.host, "ls-files").splitlines()
        self.assertIn(".clarity/setup.py", staged)
        self.assertIn(".clarity/current/rules.md", staged)
        self.assertFalse(any("/.local/" in path for path in staged))
        self.assertEqual(self.git(self.host, "show", ":business.txt"), "staged business edit")
        self.assertEqual((self.host / "business.txt").read_text(), "unstaged business edit\n")
        indexed_make = self.git(self.host, "show", ":Makefile")
        self.assertIn("Clarity sidecar", indexed_make)
        self.assertIn("existing-build", indexed_make)
        self.assertIn("STAGED_BUSINESS = yes", indexed_make)
        self.assertNotIn("UNSTAGED_BUSINESS", indexed_make)
        self.assertIn("UNSTAGED_BUSINESS = yes", (self.host / "Makefile").read_text())
        self.assertTrue(self.read_manifest(self.host)["track_git"])

    def test_existing_installation_defaults_to_update_and_skips_tracking_question(self):
        self.launch(self.host, "n\n")
        rule = self.host / ".clarity/current/rules.md"
        rule.write_text("keep these project rules\n")
        (self.source / "engine/new-version.txt").write_text("next version\n")
        latest = self.commit("next version")
        result = self.launch(self.host, "\ny\n")
        self.assertIn("更新（默认）", result.stdout)
        self.assertNotIn("Git 跟踪", result.stdout)
        self.assertEqual(self.read_manifest(self.host)["commit"], latest)
        self.assertEqual(rule.read_text(), "keep these project rules\n")
        self.assertEqual(self.git(self.host, "ls-files", "--", ".clarity"), "")

    def test_full_install_choice_reaches_tracking_question_without_resetting_current(self):
        self.launch(self.host, "n\n")
        rule = self.host / ".clarity/current/rules.md"
        rule.write_text("keep registry on reinstall\n")
        result = self.launch(self.host, "2\ny\n")
        self.assertLess(result.stdout.index("更新（默认）"), result.stdout.index("Git 跟踪"))
        self.assertIn(".clarity/setup.py", self.git(self.host, "ls-files").splitlines())
        self.assertEqual(rule.read_text(), "keep registry on reinstall\n")
        self.assertNotIn("/.clarity/", (self.host / ".git/info/exclude").read_text().splitlines())

    def test_index_write_failure_rolls_back_deployment_and_prior_business_staging(self):
        import argparse
        import importlib.util
        from unittest.mock import patch

        self.prepare_business_changes()
        previous = (self.host / ".git/index").read_bytes()
        specification = importlib.util.spec_from_file_location("flow_setup", fixture.FACTORY / "setup.py")
        setup = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(setup)
        original_write = setup.write_file
        failed = False

        def fail_index(path, value):
            nonlocal failed
            if path == self.host / ".git/index" and not failed:
                failed = True
                raise OSError("synthetic index write failure")
            return original_write(path, value)

        arguments = argparse.Namespace(command="install", repo=str(self.host), source=str(self.source), ref=None, track="yes")
        with patch.object(setup, "write_file", side_effect=fail_index):
            with self.assertRaisesRegex(OSError, "synthetic index write failure"):
                setup.install(arguments)
        self.assertEqual((self.host / ".git/index").read_bytes(), previous)
        self.assertFalse((self.host / ".git/index.lock").exists())
        self.assertFalse((self.host / ".clarity").exists())
        self.assertEqual((self.host / "business.txt").read_text(), "unstaged business edit\n")

    def test_installation_stays_in_current_subdirectory_of_an_existing_git_project(self):
        self.prepare_business_changes()
        child = self.host / "work area"
        child.mkdir()
        parent_make = (self.host / "Makefile").read_bytes()
        self.launch(child, "n\n")
        self.assertFalse((self.host / ".clarity").exists())
        self.assertTrue((child / ".clarity/setup.py").is_file())
        self.assertFalse((child / ".git").exists())
        self.assertEqual(self.read_manifest(child)["worktree_root"], str(child))
        self.assertEqual((self.host / "Makefile").read_bytes(), parent_make)
        self.run_command(["sh", str(child / ".clarity/update")], cwd=self.host)
        self.assertFalse((self.host / ".clarity").exists())

    def test_external_bootstrap_needs_no_installer_or_git_repository_in_target(self):
        target = self.root / "bootstrap-target"
        target.mkdir()
        binary = self.root / "bin"
        binary.mkdir()
        curl = binary / "curl"
        curl.write_text("#!/bin/sh\nwhile [ \"$#\" -gt 0 ]; do\n"
                        "  if [ \"$1\" = -o ]; then cp \"$CLARITY_BOOTSTRAP_SOURCE\" \"$2\"; exit 0; fi\n"
                        "  shift\ndone\nexit 2\n")
        curl.chmod(0o755)
        environment = dict(self.environment, PATH=str(binary) + os.pathsep + os.environ["PATH"],
                           CLARITY_BOOTSTRAP_SOURCE=str(self.source / "setup.py"))
        git_config = self.root / "bootstrap-git-config"
        self.run_command(["git", "config", "--file", str(git_config),
                          "url." + self.source.as_uri() + ".insteadOf", "https://github.com/yzhang37/code-clair.git"])
        environment["GIT_CONFIG_GLOBAL"] = str(git_config)
        result = subprocess.run(["sh", str(self.source / "install.sh"), "--source", str(self.source)],
                                cwd=target, env=environment, input="", text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((target / ".clarity/update").is_file())
        self.assertFalse((target / ".git").exists())
        self.assertEqual({p.name for p in target.iterdir()}, {".clarity"})


if __name__ == "__main__":
    unittest.main()
