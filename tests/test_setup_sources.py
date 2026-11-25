#!/usr/bin/env python3
"""test_setup_sources.py() 用本地 Git 代替网络，核对默认来源、来源切换及宿主克隆后的恢复。

HTTPS 地址只作为登记来源；临时 Git 配置将它映射到临时原厂，测试不访问 GitHub。
"""

import json
import sys
import unittest

import test_setup as setup_fixture


DEFAULT_SOURCE = "https://github.com/yzhang37/code-clair.git"


class SetupSourceTests(unittest.TestCase):
    """SetupSourceTests() 复用临时仓库工具，不继承另一套安装测试。"""

    run_command = setup_fixture.SidecarSetupTests.run_command
    git = setup_fixture.SidecarSetupTests.git
    initialize = setup_fixture.SidecarSetupTests.initialize
    new_host = setup_fixture.SidecarSetupTests.new_host
    commit = setup_fixture.SidecarSetupTests.commit
    manifest = setup_fixture.SidecarSetupTests.manifest
    installed_bytes = setup_fixture.SidecarSetupTests.installed_bytes
    directory_state = setup_fixture.SidecarSetupTests.directory_state

    def setUp(self):
        setup_fixture.SidecarSetupTests.setUp(self)
        git_config = self.root / "offline-git-config"
        self.environment["GIT_CONFIG_GLOBAL"] = str(git_config)
        self.run_command(["git", "config", "--file", str(git_config),
                          "url." + self.source.as_uri() + ".insteadOf", DEFAULT_SOURCE])

    def install(self, *, source=None, check=True):
        arguments = [sys.executable, str(self.source / "setup.py"), "install", "--repo", str(self.host), "--track", "yes"]
        if source is not None:
            arguments.extend(["--source", str(source)])
        return self.run_command(arguments, check=check)

    def update(self, *, source=None, ref=None, check=True):
        arguments = [sys.executable, str(self.host / ".clarity/setup.py"), "update", "--repo", str(self.host)]
        if source is not None:
            arguments.extend(["--source", str(source)])
        if ref is not None:
            arguments.extend(["--ref", ref])
        return self.run_command(arguments, check=check)

    def test_default_https_is_recorded_and_reused_without_treating_git_rewrite_as_a_source_change(self):
        self.install()
        self.assertEqual(self.manifest()["source"], DEFAULT_SOURCE)
        self.assertEqual(self.directory_state(self.host / ".clarity/current"),
                         self.directory_state(self.source / "current"))
        current = self.host / ".clarity/current/rules.md"
        current.write_bytes(b"Project-owned rules stay local.\n")
        workflow = self.source / "engine/workflow.md"
        workflow.write_bytes(workflow.read_bytes() + b"\nDefault upstream advanced.\n")
        latest = self.commit("advance default upstream")
        self.update()
        self.assertEqual(self.manifest()["source"], DEFAULT_SOURCE)
        self.assertEqual(self.manifest()["commit"], latest)
        self.assertEqual((self.host / ".clarity/engine/workflow.md").read_bytes(), workflow.read_bytes())
        self.assertEqual(current.read_bytes(), b"Project-owned rules stay local.\n")

    def test_first_install_uses_current_from_the_selected_local_source_commit(self):
        local = self.root / "alternate-first-install-factory"
        self.run_command(["git", "clone", "--quiet", str(self.source), str(local)], cwd=self.root)
        self.git(local, "config", "user.name", "Offline Alternate Factory")
        self.git(local, "config", "user.email", "alternate-fixture@example.invalid")
        self.git(local, "config", "commit.gpgsign", "false")
        marker = local / "current/selected-source.txt"
        marker.write_bytes("已选本地发行版本\r\n".encode("utf-8"))
        (local / "initial/source-marker.txt").write_bytes(b"selected source factory initial\n")
        workflow = local / "engine/workflow.md"
        workflow.write_bytes(workflow.read_bytes() + b"\nSelected alternate factory release.\n")
        release = setup_fixture.publish_factory(self, local, "selected source with distinct current")
        expected = {directory: self.directory_state(local / directory)
                    for directory in ("engine", "initial", "current")}
        marker.write_bytes(b"dirty local source is not the selected release\n")
        self.install(source=local)
        self.assertEqual(self.manifest()["source"], str(local))
        self.assertEqual(self.manifest()["commit"], release)
        for directory, state in expected.items():
            self.assertEqual(self.directory_state(self.host / ".clarity" / directory), state)
        self.assertFalse(any(name.startswith("current/") for name in self.manifest()["files"]))

    def test_cloned_host_bootstraps_missing_local_state_and_preserves_its_tracked_current(self):
        self.install()
        first_host = self.host
        current = first_host / ".clarity/current/rules.md"
        current.write_bytes(b"These project rules travel with the host Git repository.\n")
        self.git(first_host, "add", ".clarity", "Makefile")
        self.git(first_host, "commit", "-q", "-m", "track installed sidecar and project materials")
        cloned = self.root / "cloned-host"
        self.run_command(["git", "clone", "--quiet", str(first_host), str(cloned)], cwd=self.root)
        self.host = cloned
        self.assertFalse((cloned / ".git/clarity").exists())
        sidecar_make = cloned / ".clarity/Makefile"
        expected_make = sidecar_make.read_bytes()
        sidecar_make.write_bytes(expected_make + b"\n# A local edit must not be silently adopted.\n")
        changed = self.installed_bytes()
        refused = self.update(check=False)
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertEqual(self.installed_bytes(), changed)
        self.assertFalse((cloned / ".git/clarity/installation.json").exists())
        sidecar_make.write_bytes(expected_make)
        workflow = self.source / "engine/workflow.md"
        workflow.write_bytes(workflow.read_bytes() + b"\nFactory changed after host cloning.\n")
        latest = self.commit("advance default before bootstrap")
        self.update()
        self.assertEqual(self.manifest()["source"], DEFAULT_SOURCE)
        self.assertEqual(self.manifest()["commit"], latest)
        self.assertEqual((cloned / ".clarity/current/rules.md").read_bytes(), current.read_bytes())
        self.assertEqual((cloned / ".clarity/engine/workflow.md").read_bytes(), workflow.read_bytes())
        self.assertEqual(sidecar_make.read_bytes(), expected_make)
        self.assertEqual((cloned / "Makefile").read_bytes().count(b"include .clarity/Makefile"), 1)
        self.assertFalse((cloned / ".git/clarity/config.json").exists())
        self.assertFalse((cloned / ".git/clarity/engine").exists())
        self.assertFalse((cloned / ".git/hooks/commit-msg").exists())
        self.assertFalse((cloned / ".git/hooks/pre-commit").exists())

    def test_explicit_local_source_can_be_selected_and_then_changed_back_to_https(self):
        self.install()
        default_commit = self.manifest()["commit"]
        default_version = json.loads((self.source / "engine/release.json").read_text())["product_version"]
        current = self.host / ".clarity/current/rules.md"
        current.write_bytes(b"Host materials remain the same across factory source choices.\n")
        current_state = self.directory_state(current.parent)
        local = self.root / "alternate-factory"
        self.run_command(["git", "clone", "--quiet", str(self.source), str(local)], cwd=self.root)
        self.git(local, "config", "user.name", "Offline Alternate Factory")
        self.git(local, "config", "user.email", "alternate-fixture@example.invalid")
        self.git(local, "config", "commit.gpgsign", "false")
        workflow = local / "engine/workflow.md"
        workflow.write_bytes(workflow.read_bytes() + b"\nAlternate local source version.\n")
        (local / "initial/source-marker.txt").write_bytes(b"alternate factory initial\n")
        (local / "current/rules.md").write_bytes(b"Alternate upstream seed must not replace host rules.\n")
        (local / "current/source-note.txt").write_bytes(b"do not merge source current\n")
        local_commit = setup_fixture.publish_factory(self, local, "alternate factory version")
        self.update(source=local)
        self.assertEqual(self.manifest()["source"], str(local))
        self.assertEqual(self.manifest()["commit"], local_commit)
        self.assertEqual((self.host / ".clarity/engine/workflow.md").read_bytes(), workflow.read_bytes())
        self.assertEqual(self.directory_state(self.host / ".clarity/initial"),
                         self.directory_state(local / "initial"))
        self.assertEqual(self.directory_state(current.parent), current_state)
        self.update(source=DEFAULT_SOURCE, ref="v" + default_version)
        self.assertEqual(self.manifest()["source"], DEFAULT_SOURCE)
        self.assertEqual(self.manifest()["commit"], default_commit)
        self.assertEqual((self.host / ".clarity/engine/workflow.md").read_bytes(),
                         (self.source / "engine/workflow.md").read_bytes())
        self.assertEqual(current.read_bytes(), b"Host materials remain the same across factory source choices.\n")
        self.assertEqual(self.directory_state(self.host / ".clarity/initial"),
                         self.directory_state(self.source / "initial"))
        self.assertEqual(self.directory_state(current.parent), current_state)

    def test_failed_explicit_switch_keeps_old_origin_current_configuration_and_runtime(self):
        self.install(source=self.source)
        current = self.host / ".clarity/current/rules.md"
        current.write_bytes(b"Guard empty sequences before indexing their first element.\n")
        self.run_command([sys.executable, str(self.host / ".clarity/engine/runner/install.py"),
                          "--repo", str(self.host), "--basis", ".clarity/current/rules.md",
                          "--codex-bin", sys.executable, "--model", "source-switch-fixture",
                          "--reviewers", "3", "--timeout", "43"])
        config_path = self.host / ".git/clarity/config.json"
        config = json.loads(config_path.read_text())
        config["future_setting"] = {"preserve_on_failed_switch": True}
        config_path.write_text(json.dumps(config) + "\n")
        current.write_bytes(b"Changed project rules stay candidates even if switching fails.\n")
        before = self.installed_bytes()
        runtime = self.host / ".git/clarity/engine"
        previous_runtime = {str(path.relative_to(runtime)): path.read_bytes()
                            for path in runtime.rglob("*") if path.is_file()}
        previous_hook = (self.host / ".git/hooks/pre-commit").read_bytes()
        result = self.update(source=self.root / "missing-factory", check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.installed_bytes(), before)
        self.assertEqual({str(path.relative_to(runtime)): path.read_bytes()
                          for path in runtime.rglob("*") if path.is_file()}, previous_runtime)
        self.assertEqual((self.host / ".git/hooks/pre-commit").read_bytes(), previous_hook)
        self.assertEqual(self.manifest()["source"], str(self.source))
        self.update()
        self.assertEqual(self.manifest()["source"], str(self.source))
        self.assertEqual(json.loads(config_path.read_text()), config)
        self.assertEqual(current.read_bytes(), b"Changed project rules stay candidates even if switching fails.\n")


if __name__ == "__main__":
    unittest.main()
