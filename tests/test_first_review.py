"""test_first_review.py 从真实安装入口接到第一次审核，不以复制文件成功代替可使用。

离线模型仅核对编排、原文传递与 Git 状态；不宣称它验证真实模型的语义判断。
"""

import json
import os
from pathlib import Path
import shutil
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
    directory_state = fixture.SidecarSetupTests.directory_state

    def setUp(self):
        fixture.SidecarSetupTests.setUp(self)
        self.environment['CLARITY_LANG'] = 'en'
        self.environment['OPENAI_API_KEY'] = 'synthetic-offline-fixture-key'
        self.binary = self.root / 'bin'
        self.binary.mkdir()
        self.fake = self.binary / 'codex'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE_CODEX)
        self.fake.chmod(0o755)
        self.environment.update(CLARITY_AGENT='codex', CLARITY_AGENT_COMMAND=json.dumps([str(self.fake)]))
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

    def fill_rules(self, rule=None):
        """fill_rules() 为采用关系测试隔离规则输入，不把原厂案例的语义结果作为这些测试的前提。"""
        rule = rule or self.rule
        rule.write_text('Guard empty input before indexing values.\n')
        shutil.rmtree(rule.parent / 'cases')
        (rule.parent / 'cases').mkdir()

    def calls(self):
        return [json.loads(path.read_text()) for path in (self.binary / 'calls').glob('*.json')]

    def set_preferences(self):
        config_path = self.state / 'config.json'
        config = json.loads(config_path.read_text())
        config.update(model='offline-owner-choice', reviewers=3, timeout=47,
                      owner_preference={'keep': ['explicit', 'settings']})
        config_path.write_text(json.dumps(config))
        return config

    def test_untouched_install_adopts_release_current_without_activating_hooks(self):
        expected = self.directory_state(self.source / 'current')
        self.assertEqual(self.directory_state(self.rule.parent), expected)
        result = self.run_command(['make', 'review'], check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.result()[0]['status'], 'issues')
        config = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(config['basis_source'], 'local')
        self.assertEqual(config['basis_snapshot']['.clarity/current/rules.md']['text'],
                         (self.source / 'current/rules.md').read_text())
        self.assertEqual(set(config['basis_snapshot']), {
            '.clarity/current/' + path.relative_to(self.source / 'current').as_posix()
            for path in (self.source / 'current').rglob('*') if path.is_file()})
        self.assertEqual(self.directory_state(self.rule.parent), expected)
        self.assertEqual((self.host / '.git/index').read_bytes(), self.index)
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
        config = self.set_preferences()
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
        self.assertEqual(updated, config)
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
        for call in self.calls():
            if call['stage'] == 'health':
                self.assertEqual(call['packet']['adopted_rules']['.clarity/current/rules.md'], '1: Guard empty input before indexing values.')
            if call['stage'].startswith('reviewer-'):
                self.assertEqual(set(call['current_files']), {'legacy.py'})

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
        self.fill_rules(child / '.clarity/current/rules.md')
        self.run_command(['make', 'review'], cwd=child, check=False)
        report, _ = self.result()
        self.assertEqual(report['status'], 'issues')
        config = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(config['basis_worktree'], str(self.host))
        self.assertEqual(config['cases_root'], 'work area/.clarity/current/cases/')
        self.assertEqual(set(config['basis_snapshot']), {'work area/.clarity/current/rules.md'})

    def test_nested_current_recovery_resets_shared_adoption_without_sibling_interference(self):
        child = self.host / 'nested work area'
        child.mkdir()
        self.run_command([sys.executable, str(self.source / 'setup.py'), 'install', '--repo', str(child),
                          '--source', str(self.source), '--track', 'no'])
        rule = child / '.clarity/current/rules.md'
        self.fill_rules(rule)
        self.run_command(['make', 'review'], cwd=child, check=False)
        self.assertEqual(self.result()[0]['status'], 'issues')
        old = self.set_preferences()
        config_path = self.state / 'config.json'
        original_config = config_path.read_bytes()
        sibling = self.host / 'unrelated sibling'
        sibling.mkdir()
        self.run_command([sys.executable, str(self.source / 'setup.py'), 'install', '--repo', str(sibling),
                          '--source', str(self.source), '--track', 'no'])
        self.assertEqual(config_path.read_bytes(), original_config)
        shutil.rmtree(rule.parent)
        self.run_command([str(child / '.clarity/update')], cwd=child)
        self.assertEqual(self.directory_state(rule.parent), self.directory_state(self.source / 'initial'))
        reset = json.loads(config_path.read_text())
        self.assertTrue(reset['basis_reset'])
        self.assertEqual(reset['sidecar_project'], str(child))
        self.assertFalse({'basis', 'basis_hashes', 'basis_snapshot'} & reset.keys())
        for name in ('agent', 'agent_command', 'model', 'reviewers', 'timeout', 'owner_preference'):
            self.assertEqual(reset[name], old[name])
        self.run_command(['make', 'review'], cwd=child, check=False)
        self.assertEqual(self.result()[0]['status'], 'issues')
        adopted = json.loads(config_path.read_text())
        key = 'nested work area/.clarity/current/rules.md'
        self.assertNotIn('basis_reset', adopted)
        self.assertEqual(adopted['basis_snapshot'][key]['text'], (self.source / 'initial/rules.md').read_text())
        self.assertNotEqual(adopted['basis_snapshot'], old['basis_snapshot'])
        self.assertEqual(adopted['basis_worktree'], str(self.host))
        for name in ('agent', 'agent_command', 'model', 'reviewers', 'timeout', 'owner_preference'):
            self.assertEqual(adopted[name], old[name])
        self.assertEqual(self.directory_state(rule.parent), self.directory_state(self.source / 'initial'))
        active_config = config_path.read_bytes()
        self.run_command([str(sibling / '.clarity/update')], cwd=sibling)
        self.assertEqual(config_path.read_bytes(), active_config)

    def test_new_manual_review_adopts_local_rules_even_when_head_has_older_rules(self):
        self.fill_rules()
        old = 'Old committed criteria must not replace the explicitly reviewed local criteria.\n'
        self.rule.write_text(old)
        self.git(self.host, 'add', '-f', '.clarity/current/rules.md')
        self.git(self.host, 'commit', '-q', '-m', 'Historical criteria')
        self.fill_rules()
        expected = self.rule.read_text()
        result = self.run_command(['make', 'review'], check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.result()[0]['status'], 'issues')
        config = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(config['basis_snapshot']['.clarity/current/rules.md']['text'], expected)
        self.assertEqual(self.git(self.host, 'show', 'HEAD:.clarity/current/rules.md'), old.strip())
        health = next(call for call in self.calls() if call['stage'] == 'health')
        self.assertEqual(health['packet']['adopted_rules']['.clarity/current/rules.md'],
                         '1: ' + expected.strip())

    def test_program_repair_preserves_current_preferences_and_adoption(self):
        self.fill_rules()
        self.run_command(['make', 'review'], check=False)
        config = self.set_preferences()
        self.rule.write_text('A local candidate remains a candidate after program repair.\n')
        (self.rule.parent / 'owner-note.txt').write_bytes(b'preserve local bytes\r\n')
        expected = self.directory_state(self.rule.parent)
        for path in (self.host / '.clarity').iterdir():
            if path.name == 'current':
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        self.run_command([sys.executable, str(self.source / 'setup.py'), 'update',
                          '--repo', str(self.host), '--source', str(self.source)])
        self.assertEqual(self.directory_state(self.rule.parent), expected)
        self.assertEqual(json.loads((self.state / 'config.json').read_text()), config)
        self.run_command(['make', 'review'], check=False)
        self.assertEqual(json.loads((self.state / 'config.json').read_text()), config)
        _, directory = self.result()
        inputs = json.loads((directory / 'input.json').read_text())
        self.assertEqual(inputs['basis_changes']['.clarity/current/rules.md']['status'], 'modified')

    def test_recreated_current_waits_for_manual_adoption_and_preserves_preferences(self):
        self.fill_rules()
        self.run_command(['make', 'review'], check=False)
        old = self.set_preferences()
        self.run_command(['make', 'review-install'])
        shutil.rmtree(self.rule.parent)
        self.run_command([str(self.host / '.clarity/update')])
        self.assertEqual(self.directory_state(self.rule.parent),
                         self.directory_state(self.source / 'initial'))
        config_path = self.state / 'config.json'
        reset = json.loads(config_path.read_text())
        self.assertTrue(reset['basis_reset'])
        self.assertFalse({'basis', 'basis_hashes', 'basis_snapshot'} & reset.keys())
        for name in ('agent', 'agent_command', 'model', 'reviewers', 'timeout', 'owner_preference'):
            self.assertEqual(reset[name], old[name])
        calls_before = self.calls()
        message = self.root / 'pending-adoption-message.txt'
        message.write_text('No automatic adoption from a commit hook.\n')
        hook = self.host / '.git/hooks/commit-msg'
        result = self.run_command(['sh', str(hook), str(message)], check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(config_path.read_text()), reset)
        self.assertEqual(self.calls(), calls_before)
        self.assertEqual(self.result()[0]['status'], 'incomplete')
        manual = self.run_command(['make', 'review'], check=False)
        self.assertNotEqual(manual.returncode, 0, manual.stdout + manual.stderr)
        self.assertEqual(self.result()[0]['status'], 'issues')
        adopted = json.loads(config_path.read_text())
        self.assertNotIn('basis_reset', adopted)
        self.assertEqual(adopted['basis_snapshot']['.clarity/current/rules.md']['text'],
                         (self.source / 'initial/rules.md').read_text())
        self.assertNotEqual(adopted['basis_snapshot'], old['basis_snapshot'])
        for name in ('agent', 'agent_command', 'model', 'reviewers', 'timeout', 'owner_preference'):
            self.assertEqual(adopted[name], old[name])

    def test_deleted_sidecar_disables_own_commit_hook_and_preserves_foreign_hook(self):
        self.fill_rules()
        self.run_command(['make', 'review'], check=False)
        foreign = b'#!/bin/sh\n# another tool owns this hook\nexit 0\n'
        pre_commit = self.host / '.git/hooks/pre-commit'
        pre_commit.write_bytes(foreign)
        pre_commit.chmod(0o755)
        self.run_command(['make', 'review-install'])
        config = (self.state / 'config.json').read_bytes()
        calls_before = self.calls()
        shutil.rmtree(self.host / '.clarity')
        self.git(self.host, 'commit', '--allow-empty', '-q', '-m', 'Sidecar deliberately removed')
        self.assertEqual(pre_commit.read_bytes(), foreign)
        self.assertEqual((self.state / 'config.json').read_bytes(), config)
        self.assertEqual(self.calls(), calls_before)
        self.assertFalse((self.host / '.clarity').exists())

    def test_deleted_sidecar_disables_bound_pre_commit_hook(self):
        self.fill_rules()
        self.run_command(['make', 'review'], check=False)
        hook = self.host / '.git/hooks/pre-commit'
        script = ('import sys\nfrom pathlib import Path\n'
                  'sys.path.insert(0, sys.argv[1])\nfrom install import hook_script\n'
                  'Path(sys.argv[2]).write_bytes(hook_script("pre-commit", Path(sys.argv[3]), Path(sys.argv[4])))\n')
        self.run_command([sys.executable, '-c', script, str(self.state / 'engine/runner'),
                          str(hook), str(self.state / 'engine'), str(self.state / 'config.json')])
        hook.chmod(0o755)
        calls_before = self.calls()
        config = (self.state / 'config.json').read_bytes()
        shutil.rmtree(self.host / '.clarity')
        self.git(self.host, 'commit', '--allow-empty', '-q', '-m', 'Bound sidecar deliberately removed')
        self.assertEqual((self.state / 'config.json').read_bytes(), config)
        self.assertEqual(self.calls(), calls_before)
        self.assertFalse((self.host / '.clarity').exists())

    def test_deleted_sidecar_hooks_ignore_invalid_retained_config_without_touching_foreign_hook(self):
        self.fill_rules()
        self.run_command(['make', 'review'], check=False)
        self.run_command(['make', 'review-install'])
        hooks = self.host / '.git/hooks'
        pre_commit = hooks / 'pre-commit'
        script = ('import sys\nfrom pathlib import Path\n'
                  'sys.path.insert(0, sys.argv[1])\nfrom install import hook_script\n'
                  'Path(sys.argv[2]).write_bytes(hook_script("pre-commit", Path(sys.argv[3]), Path(sys.argv[4])))\n')
        self.run_command([sys.executable, '-c', script, str(self.state / 'engine/runner'),
                          str(pre_commit), str(self.state / 'engine'), str(self.state / 'config.json')])
        pre_commit.chmod(0o755)
        foreign = hooks / 'post-commit'
        foreign_bytes = b'#!/bin/sh\n# another tool owns this hook\nexit 0\n'
        foreign.write_bytes(foreign_bytes)
        foreign.chmod(0o755)
        calls_before = self.calls()
        latest = (self.state / 'latest.json').read_bytes()
        shutil.rmtree(self.host / '.clarity')
        config_path = self.state / 'config.json'
        message = self.root / 'invalid-retained-config-message.txt'
        body = 'Sidecar deliberately removed; retained config does not reactivate it.\n'
        for retained in (b'{broken JSON', b'[]'):
            with self.subTest(retained=retained):
                config_path.write_bytes(retained)
                message.write_text(body)
                self.run_command(['sh', str(pre_commit)])
                self.run_command(['sh', str(hooks / 'commit-msg'), str(message)])
                self.git(self.host, 'commit', '--allow-empty', '-q', '-m', 'Removed sidecar with invalid state')
                self.assertEqual(config_path.read_bytes(), retained)
                self.assertEqual(message.read_text(), body)
                self.assertEqual((self.state / 'latest.json').read_bytes(), latest)
                self.assertEqual(foreign.read_bytes(), foreign_bytes)
                self.assertEqual(self.calls(), calls_before)
                self.assertFalse((self.host / '.clarity').exists())

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
