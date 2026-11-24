"""test_first_review.py 从真实安装入口接到第一次审核，不以复制文件成功代替可使用。

离线模型仅核对编排、原文传递与 Git 状态；不宣称它验证真实模型的语义判断。
"""

import json
import os
from pathlib import Path
import sys
import unittest

import test_setup as fixture

sys.path.insert(0, str(fixture.FACTORY / 'engine/runner'))
from test_review import FAKE_CODEX, BROKEN


class FirstReviewTests(unittest.TestCase):
    run_command = fixture.SidecarSetupTests.run_command
    git = fixture.SidecarSetupTests.git
    initialize = fixture.SidecarSetupTests.initialize
    new_host = fixture.SidecarSetupTests.new_host
    commit = fixture.SidecarSetupTests.commit

    def setUp(self):
        fixture.SidecarSetupTests.setUp(self)
        self.environment['CLARITY_LANG'] = 'en'
        self.environment['OPENAI_API_KEY'] = 'synthetic-offline-fixture-key'
        self.binary = self.root / 'bin'
        self.binary.mkdir()
        self.fake = self.binary / 'codex'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE_CODEX)
        self.fake.chmod(0o755)
        self.fake.with_suffix('.json').write_text(json.dumps({'mode': 'normal'}))
        self.environment['PATH'] = str(self.binary) + os.pathsep + self.environment.get('PATH', os.defpath)
        (self.host / 'legacy.py').write_text(BROKEN)
        self.git(self.host, 'add', 'legacy.py')
        self.git(self.host, 'commit', '-q', '-m', 'Existing code, without a Makefile')
        self.index = (self.host / '.git/index').read_bytes()
        self.run_command([sys.executable, str(self.source / 'setup.py'), 'install', '--source', str(self.source), '--track', 'no'])
        self.assertEqual((self.host / '.git/index').read_bytes(), self.index)
        self.state = self.host / '.git/clarity'
        self.rule = self.host / '.clarity/current/rules.md'

    def result(self):
        pointer = json.loads((self.state / 'latest.json').read_text())
        directory = Path(pointer['run'])
        return json.loads((directory / 'result.json').read_text()), directory

    def fill_rules(self):
        self.rule.write_text('Guard empty input before indexing values.\n')

    def test_untouched_install_reports_the_missing_owner_rules_without_activating_hooks(self):
        result = self.run_command(['make', 'review'], check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Fill .clarity/current/rules.md', result.stdout + result.stderr)
        self.assertEqual(self.result()[0]['status'], 'incomplete')
        self.assertFalse((self.state / 'config.json').exists())
        self.assertFalse((self.host / '.git/hooks/pre-commit').exists())
        self.assertFalse((self.host / '.git/hooks/commit-msg').exists())

    def test_local_manual_review_preserves_adoption_and_explicit_hook_activation_preserves_config(self):
        self.fill_rules()
        hooks = self.host / '.git/hooks'
        foreign = b'#!/bin/sh\n# another tool owns this hook\nexit 0\n'
        (hooks / 'pre-commit').write_bytes(foreign)
        original_index = self.git(self.host, 'ls-files', '--stage')
        first = self.run_command(['make', 'review'], check=False)
        self.assertIn('BLOCK: code-review findings', first.stdout + first.stderr)
        report, directory = self.result()
        self.assertEqual(report['status'], 'issues')
        config_file = self.state / 'config.json'
        config = json.loads(config_file.read_text())
        self.assertEqual(config['basis_source'], 'local')
        self.assertEqual(set(config['basis_snapshot']), {'.clarity/current/rules.md'})
        self.assertEqual((hooks / 'pre-commit').read_bytes(), foreign)
        self.assertFalse((hooks / 'commit-msg').exists())
        self.assertEqual(self.git(self.host, 'ls-files', '--stage'), original_index)
        self.assertEqual(self.git(self.host, 'ls-files', '--', '.clarity'), '')
        self.assertEqual(json.loads((directory / 'fixtures.json').read_text())['status'], 'no_coverage')
        self.rule.write_text('Changed candidate rules must not replace the adopted rules.\n')
        previous_config = config_file.read_bytes()
        self.run_command(['make', 'review-staged'], check=False)
        _, changed_directory = self.result()
        inputs = json.loads((changed_directory / 'input.json').read_text())
        self.assertEqual(inputs['basis_changes']['.clarity/current/rules.md']['status'], 'modified')
        self.assertEqual(config_file.read_bytes(), previous_config)
        self.run_command([str(self.host / '.clarity/update')])
        updated = json.loads(config_file.read_text())
        self.assertEqual(updated['basis_snapshot'], config['basis_snapshot'])
        self.assertEqual(updated['basis_hashes'], config['basis_hashes'])
        self.git(self.host, 'config', 'core.hooksPath', '.custom-hooks/nested')
        previous_config = config_file.read_bytes()
        self.run_command(['make', 'review-install'])
        self.assertEqual(config_file.read_bytes(), previous_config)
        installed_hook = self.host / '.custom-hooks/nested/commit-msg'
        self.assertTrue(installed_hook.is_file())
        self.assertEqual((hooks / 'pre-commit').read_bytes(), foreign)
        message = self.root / 'message.txt'
        message.write_text('Commit fixture\n\nReview-Bypass: Retain all findings.\n')
        self.run_command(['sh', str(installed_hook), str(message)])
        self.assertIn('legacy.py:', message.read_text())
        self.assertEqual(self.git(self.host, 'ls-files', '--stage'), original_index)
        for path in (self.binary / 'calls').glob('*.json'):
            call = json.loads(path.read_text())
            if call['stage'] == 'health':
                self.assertEqual(call['packet']['adopted_rules']['.clarity/current/rules.md'], '1: Guard empty input before indexing values.')
            if call['stage'].startswith('reviewer-'):
                self.assertEqual(set(call['packet']['current_files']), {'legacy.py'})

    def test_local_material_change_during_model_call_never_publishes_a_pass(self):
        self.fill_rules()
        replacement = FAKE_CODEX.replace('calls.mkdir(exist_ok=True)',
            'calls.mkdir(exist_ok=True)\nif stage == "health":\n    Path(' + repr(str(self.rule)) + ').write_text("Changed during review.\\n")')
        self.fake.write_text('#!' + sys.executable + '\n' + replacement)
        self.run_command(['make', 'review'], check=False)
        report, _ = self.result()
        self.assertEqual(report['status'], 'incomplete')
        self.assertIn('Local review materials changed', report['incomplete_reason'])

    def test_update_backfills_missing_loader_without_staging_the_sidecar(self):
        makefile = self.host / 'Makefile'
        makefile.unlink()
        record_path = self.state / 'installation.json'
        record = json.loads(record_path.read_text())
        record['host_makefile'] = None
        record_path.write_text(json.dumps(record))
        self.run_command([str(self.host / '.clarity/update')])
        self.assertIn('include .clarity/Makefile', makefile.read_text())
        self.assertEqual((self.host / '.git/index').read_bytes(), self.index)
        self.assertEqual(self.git(self.host, 'ls-files', '--', '.clarity'), '')

    def test_first_review_from_installed_subdirectory_binds_paths_to_git_root(self):
        child = self.host / 'work area'
        child.mkdir()
        self.run_command([sys.executable, str(self.source / 'setup.py'), 'install', '--repo', str(child),
                          '--source', str(self.source), '--track', 'no'])
        (child / '.clarity/current/rules.md').write_text('Guard empty input before indexing values.\n')
        self.run_command(['make', 'review'], cwd=child, check=False)
        report, _ = self.result()
        self.assertEqual(report['status'], 'issues')
        config = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(config['basis_worktree'], str(self.host))
        self.assertEqual(config['cases_root'], 'work area/.clarity/current/cases/')
        self.assertEqual(set(config['basis_snapshot']), {'work area/.clarity/current/rules.md'})

    def test_local_adoption_cannot_silently_move_to_another_linked_worktree(self):
        self.fill_rules()
        self.run_command(['make', 'review'], check=False)
        config = (self.state / 'config.json').read_bytes()
        other = self.root / 'linked-worktree'
        self.git(self.host, 'worktree', 'add', '-q', '-b', 'other-worktree', str(other))
        result = self.run_command([sys.executable, str(self.host / '.clarity/engine/runner/review.py'),
                                   'review', '--target', 'HEAD', '--repo', str(other)], check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('belong to another worktree', result.stdout + result.stderr)
        self.assertEqual((self.state / 'config.json').read_bytes(), config)

    def test_first_review_rejects_symlinked_material_ancestors_before_adoption(self):
        self.fill_rules()
        current = self.host / '.clarity/current'
        outside = self.root / 'other-materials'
        current.rename(outside)
        current.symlink_to(outside, target_is_directory=True)
        result = self.run_command(['make', 'review'], check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symbolic link', result.stdout + result.stderr)
        self.assertFalse((self.state / 'config.json').exists())


if __name__ == '__main__':
    unittest.main()
