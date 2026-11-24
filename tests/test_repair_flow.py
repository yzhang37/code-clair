"""test_repair_flow.py 从安装入口核对手动删文件后的资料边界和默认升级选择。"""

import json
import shutil
import sys
import unittest

import test_install_flow as flow
import test_setup as fixture


class RepairFlowTests(unittest.TestCase):
    run_command = fixture.SidecarSetupTests.run_command
    git = fixture.SidecarSetupTests.git
    initialize = fixture.SidecarSetupTests.initialize
    new_host = fixture.SidecarSetupTests.new_host
    commit = fixture.SidecarSetupTests.commit
    directory_state = fixture.SidecarSetupTests.directory_state
    launch = flow.InstallFlowTests.launch
    read_manifest = flow.InstallFlowTests.read_manifest
    committed_files = flow.InstallFlowTests.committed_files
    file_bytes = flow.InstallFlowTests.file_bytes
    prepare_business_changes = flow.InstallFlowTests.prepare_business_changes

    def setUp(self):
        fixture.SidecarSetupTests.setUp(self)
        self.environment["CLARITY_LANG"] = "en"

    def remove_factory_programs(self, target):
        """remove_factory_programs() 模拟用户只留下 current，连本机安装副本也不作为恢复前提。"""
        for path in (target / ".clarity").iterdir():
            if path.name == "current":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    def update(self, target):
        return self.run_command([sys.executable, str(self.source / "setup.py"), "update",
                                 "--repo", str(target), "--source", str(self.source)], cwd=target)

    def assert_factory_release(self, target, revision="HEAD"):
        for directory in ("engine", "initial"):
            self.assertEqual(self.file_bytes(target / ".clarity" / directory),
                             self.committed_files(directory, revision))

    def test_partial_factory_defaults_to_update_without_tracking_or_business_index_changes(self):
        self.prepare_business_changes()
        self.launch(self.host, "\n")
        current = self.host / ".clarity/current"
        (current / "rules.md").write_bytes(b"local rules\r\n")
        (current / "empty-user-directory").mkdir()
        preserved = self.directory_state(current)
        index = (self.host / ".git/index").read_bytes()
        makefile = (self.host / "Makefile").read_bytes()
        (self.host / ".clarity/engine/runner/install.py").unlink()
        (self.host / ".clarity/initial/rules.md").unlink()
        result = self.launch(self.host, "\ny\n")
        self.assertIn("Update (default)", result.stdout)
        self.assertNotIn("[y/N]", result.stdout)
        self.assertEqual(self.directory_state(current), preserved)
        self.assert_factory_release(self.host)
        self.assertEqual((self.host / ".git/index").read_bytes(), index)
        self.assertEqual((self.host / "Makefile").read_bytes(), makefile)
        self.assertEqual(self.run_command(["make", "-n"]).stdout.strip(), "echo existing-build")
        self.assertFalse(self.read_manifest(self.host)["track_git"])

    def test_only_current_remaining_defaults_to_update_and_refetches_selected_release(self):
        self.launch(self.host, "\n")
        current = self.host / ".clarity/current"
        (current / "rules.md").write_bytes(b"keep the surviving project standard\n")
        cases = sorted(path for path in (current / "cases").rglob("*") if path.is_file())
        self.assertTrue(cases)
        removed_case = cases[0]
        removed_case.unlink()
        expected = self.directory_state(current)
        self.remove_factory_programs(self.host)
        state = self.host / ".git/clarity"
        for name in ("source", "sources"):
            if (state / name).exists():
                shutil.rmtree(state / name)
        (self.source / "engine/recovery-marker.txt").write_bytes(b"new committed program\n")
        release = self.commit("release after manual program deletion")
        result = self.launch(self.host, "\ny\n")
        self.assertIn("Update (default)", result.stdout)
        self.assertNotIn("[y/N]", result.stdout)
        self.assertEqual(self.read_manifest(self.host)["commit"], release)
        self.assertEqual(self.directory_state(current), expected)
        self.assertFalse(removed_case.exists())
        self.assert_factory_release(self.host, release)

    def test_update_restores_an_entirely_absent_current_from_committed_initial(self):
        self.launch(self.host, "\n")
        current = self.host / ".clarity/current"
        shutil.rmtree(current)
        (self.source / "initial/recovery-marker.txt").write_bytes(b"committed factory image\r\n")
        (self.source / "current/not-a-recovery-template.txt").write_bytes(b"first-install seed only\n")
        release = self.commit("different first-install and recovery material")
        expected = self.committed_files("initial", release)
        (self.source / "initial/recovery-marker.txt").write_bytes(b"uncommitted source must be ignored\n")
        self.update(self.host)
        self.assertEqual(self.file_bytes(current), expected)
        self.assertNotIn("not-a-recovery-template.txt", self.file_bytes(current))
        self.assert_factory_release(self.host, release)

    def test_existing_empty_and_partial_current_survive_default_update_and_explicit_reinstall(self):
        for kind in ("empty", "partial"):
            with self.subTest(kind=kind):
                target = self.new_host("wizard-preserve-" + kind)
                current = target / ".clarity/current"
                current.mkdir(parents=True)
                current.chmod(0o750)
                if kind == "partial":
                    (current / "rules.md").write_bytes(b"")
                    (current / "rules.md").chmod(0o640)
                    (current / "empty-user-directory").mkdir()
                    (current / "unfinished.txt").write_bytes(b"unfinished local content\r\n")
                expected = self.directory_state(current)
                updated = self.launch(target, "\ny\n")
                self.assertIn("Update (default)", updated.stdout)
                self.assertNotIn("[y/N]", updated.stdout)
                self.assertEqual(self.directory_state(current), expected)
                self.assertEqual(current.stat().st_mode & 0o777, 0o750)
                reinstalled = self.launch(target, "2\nn\n")
                self.assertIn("[y/N]", reinstalled.stdout)
                self.assertEqual(self.directory_state(current), expected)
                self.assertEqual(current.stat().st_mode & 0o777, 0o750)

    def test_entire_sidecar_deletion_is_first_install_despite_external_manifest(self):
        business = self.host / "business.txt"
        business.write_bytes(b"existing project\n")
        self.git(self.host, "add", "business.txt")
        self.git(self.host, "commit", "-q", "-m", "existing project")
        self.launch(self.host, "\n")
        manifest = self.host / ".git/clarity/installation.json"
        self.assertTrue(manifest.is_file())
        shutil.rmtree(self.host / ".clarity")
        (self.source / "current/first-install-marker.txt").write_bytes(b"fresh release seed\n")
        release = self.commit("new seed for a genuinely absent sidecar")
        expected = self.committed_files("current", release)
        (self.source / "current/first-install-marker.txt").write_bytes(b"uncommitted seed\n")
        index = (self.host / ".git/index").read_bytes()
        result = self.launch(self.host, "\n")
        self.assertNotIn("Update (default)", result.stdout)
        self.assertIn("[y/N]", result.stdout)
        self.assertEqual(self.file_bytes(self.host / ".clarity/current"), expected)
        self.assertEqual((self.host / ".git/index").read_bytes(), index)
        self.assertEqual(self.read_manifest(self.host)["commit"], release)

    def test_non_git_utf8_target_recovers_only_programs_without_creating_git(self):
        target = self.root / "恢复 测试"
        target.mkdir()
        self.launch(target)
        current = target / ".clarity/current"
        (current / "rules.md").write_bytes("保留使用者自己的规则\r\n".encode())
        cases = sorted(path for path in (current / "cases").rglob("*") if path.is_file())
        self.assertTrue(cases)
        removed_case = cases[0]
        removed_case.unlink()
        expected = self.directory_state(current)
        self.remove_factory_programs(target)
        result = self.launch(target, "\n")
        self.assertIn("Update (default)", result.stdout)
        self.assertNotIn("[y/N]", result.stdout)
        self.assertEqual(self.directory_state(current), expected)
        self.assertFalse(removed_case.exists())
        self.assert_factory_release(target)
        self.assertFalse((target / ".git").exists())
        self.assertIn(".clarity/engine/runner/review.py", self.run_command(["make", "-n", "review"], cwd=target).stdout)

    def test_program_recovery_preserves_model_preferences_and_previous_rule_adoption(self):
        self.launch(self.host, "\n")
        rule = self.host / ".clarity/current/rules.md"
        rule.write_bytes(b"adopted project standard\n")
        self.run_command([sys.executable, str(self.host / ".clarity/engine/runner/install.py"),
                          "--repo", str(self.host), "--basis", ".clarity/current/rules.md",
                          "--codex-bin", sys.executable, "--model", "offline-recovery-model",
                          "--reviewers", "3", "--timeout", "47"])
        config_path = self.host / ".git/clarity/config.json"
        config = json.loads(config_path.read_text())
        config["project_preference"] = {"preserve": True}
        config_path.write_text(json.dumps(config) + "\n")
        rule.write_bytes(b"candidate edit stays unadopted after program recovery\n")
        expected_current = self.directory_state(rule.parent)
        self.remove_factory_programs(self.host)
        (self.source / "engine/recovery-program.txt").write_bytes(b"committed repaired version\n")
        self.commit("program refresh without material adoption")
        self.launch(self.host, "\n")
        actual = json.loads(config_path.read_text())
        self.assertEqual({key: value for key, value in actual.items() if key != "engine_digest"},
                         {key: value for key, value in config.items() if key != "engine_digest"})
        self.assertEqual(self.directory_state(rule.parent), expected_current)


if __name__ == "__main__":
    unittest.main()
