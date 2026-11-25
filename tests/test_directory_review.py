"""test_directory_review.py 验证普通目录可直接审核，不创建 Git 仓库。

确定性 ACP 服务只验证取材、输入冻结和配置保持，不认证真实模型的语义判断。
"""

import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import unittest

import test_setup as fixture
from test_acp_flow import FAKE_ACP


DIRECTORY_ACP = FAKE_ACP.replace(
    '        record.update(health=health,',
    '''        mutation = script.with_suffix(".mutation.json")
        if health and mutation.exists():
            changed = json.loads(mutation.read_text())
            Path(changed["target"]).write_text(changed["content"])
            mutation.unlink()
        record.update(health=health,''',
)


class DirectoryReviewTests(unittest.TestCase):
    """DirectoryReviewTests() 将真实安装和 Make 入口限制在独立的非 Git 宿主中。"""

    run_command = fixture.SidecarSetupTests.run_command
    git = fixture.SidecarSetupTests.git
    initialize = fixture.SidecarSetupTests.initialize
    new_host = fixture.SidecarSetupTests.new_host
    commit = fixture.SidecarSetupTests.commit
    directory_state = fixture.SidecarSetupTests.directory_state

    def setUp(self):
        fixture.SidecarSetupTests.setUp(self)
        self.host = self.root / '普通 project'
        self.host.mkdir()
        self.environment['CLARITY_LANG'] = 'en'
        self.run_command([sys.executable, str(self.source / 'setup.py'), 'install',
                          '--source', str(self.source), '--track', 'no'])
        self.install_path = self.environment.get('PATH', os.defpath)
        self.binary = self.root / 'directory-bin'
        self.binary.mkdir()
        for name in ('make', 'sh', 'dirname', 'mkdir', 'rm', 'cp', 'chmod', 'sed'):
            (self.binary / name).symlink_to(shutil.which(name))
        python = self.binary / 'python3'
        python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys._base_executable) + ' "$@"\n')
        python.chmod(0o755)
        self.fake = self.binary / 'directory-acp.py'
        self.fake.write_text('#!' + sys.executable + '\n' + DIRECTORY_ACP)
        self.fake.chmod(0o755)
        self.environment.update(
            PATH=str(self.binary), CLARITY_AGENT='custom',
            CLARITY_AGENT_COMMAND=json.dumps([sys.executable, str(self.fake)]),
            CLARITY_PYTHON=os.environ.get('CLARITY_PYTHON', sys.executable),
        )
        self.rule = self.host / '.clarity/current/rules.md'

    def fill_rules(self):
        """fill_rules() 用自足测试规则隔离传输验证，发行资料在原厂目录保持原样。"""
        self.rule.write_text('Return the supplied value without changing it.\n')
        cases = self.rule.parent / 'cases'
        shutil.rmtree(cases)
        cases.mkdir()

    def calls(self):
        return [json.loads(path.read_text()) for path in self.fake.with_suffix('.calls').glob('*.json')]

    def update(self):
        """update() 为获取发行版本恢复 Git 命令，普通宿主本身仍不建立 Git 仓库。"""
        previous = self.environment['PATH']
        self.environment['PATH'] = self.install_path
        try:
            return self.run_command([str(self.host / '.clarity/update')])
        finally:
            self.environment['PATH'] = previous

    def report(self, completed):
        """report() 从真实终端指针回读结果，不假设非 Git 记录采用某个内部目录名。"""
        markers = [line.split('=', 1)[1] for line in completed.stdout.splitlines()
                   if line.startswith('CLARITY_REPORT_DIR=')]
        self.assertTrue(markers, completed.stdout + completed.stderr)
        directory = Path(markers[-1])
        return json.loads((directory / 'result.json').read_text()), directory

    def test_plain_directory_reviews_files_without_a_git_executable_or_repository(self):
        self.assertFalse((self.host / '.git').exists())
        self.assertIsNone(shutil.which('git', path=self.environment['PATH']))
        self.fill_rules()
        code = self.host / 'identity.py'
        code.write_text('def identity(value):\n    return value\n')
        (self.host / '.gitignore').write_text('ignored/\n')
        (self.host / 'ignored').mkdir()
        (self.host / 'ignored/unselected.py').write_text('MUST_NOT_ENTER_REVIEW = True\n')
        current = self.directory_state(self.rule.parent)
        completed = self.run_command(['make', 'review'])
        result, directory = self.report(completed)
        self.assertEqual(result['status'], 'clear')
        source = json.loads((directory / 'input.json').read_text())
        self.assertEqual(set(source['files']), {'.gitignore', 'identity.py'})
        prompts = [call for call in self.calls() if call['method'] == 'session/prompt']
        self.assertEqual(len(prompts), 5)
        self.assertEqual(sum(call['health'] for call in prompts), 1)
        self.assertTrue(all(call['covered_paths'] == ['.gitignore', 'identity.py']
                            for call in prompts if not call['health']))
        self.assertEqual(self.directory_state(self.rule.parent), current)
        self.assertEqual(code.read_text(), 'def identity(value):\n    return value\n')
        self.assertFalse((self.host / '.git').exists())

    def test_empty_installed_directory_does_not_call_a_model_or_claim_code_passed(self):
        current = self.directory_state(self.rule.parent)
        for key in ('CLARITY_AGENT', 'CLARITY_AGENT_COMMAND'):
            self.environment.pop(key, None)
        self.environment['CLARITY_PYTHON'] = str(self.root / 'must-not-bootstrap-python')
        completed = self.run_command(['make', 'review'])
        text = completed.stdout + completed.stderr
        self.assertIn('nothing to review', text.lower())
        self.assertNotIn('PASS:', text)
        self.assertNotIn('"status": "clear"', text)
        self.assertFalse(self.calls())
        self.assertEqual(self.directory_state(self.rule.parent), current)
        self.assertFalse((self.host / '.git').exists())

    def test_nested_unicode_files_and_saved_preferences_are_preserved(self):
        self.fill_rules()
        code = self.host / '代码 with spaces' / '标识.py'
        code.parent.mkdir()
        code.write_text('def identity(value):\n    return value\n')
        completed = self.run_command(['make', 'review'])
        result, directory = self.report(completed)
        self.assertEqual(result['status'], 'clear')
        source = json.loads((directory / 'input.json').read_text())
        self.assertEqual(set(source['files']), {'代码 with spaces/标识.py'})
        self.assertEqual(source['target_repository']['worktree_root'], str(self.host))
        state = directory.parent.parent
        configuration = state / 'config.json'
        config = json.loads(configuration.read_text())
        config.update(model='offline/owner', timeout=47, reviewers=3,
                      owner_preference={'keep': 'directory review'})
        configuration.write_text(json.dumps(config))
        saved = configuration.read_bytes()
        self.environment.update(CLARITY_AGENT='pi', CLARITY_AGENT_COMMAND='["must-not-run"]')
        again = self.run_command(['make', 'review'])
        self.assertEqual(self.report(again)[0]['status'], 'clear')
        self.assertEqual(configuration.read_bytes(), saved)
        self.assertEqual(code.read_text(), 'def identity(value):\n    return value\n')
        self.assertFalse((self.host / '.git').exists())

    def test_a_changed_directory_file_cannot_reuse_the_frozen_review_result(self):
        self.fill_rules()
        code = self.host / 'identity.py'
        code.write_text('def identity(value):\n    return value\n')
        changed = 'def identity(value):\n    return None\n'
        self.fake.with_suffix('.mutation.json').write_text(json.dumps({'target': str(code), 'content': changed}))
        completed = self.run_command(['make', 'review'], check=False)
        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result, _ = self.report(completed)
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('changed', result['incomplete_reason'].lower())
        self.assertEqual(code.read_text(), changed)
        self.assertFalse((self.host / '.git').exists())

    def test_program_update_and_current_recovery_keep_directory_review_preferences(self):
        self.fill_rules()
        (self.host / 'identity.py').write_text('def identity(value):\n    return value\n')
        first = self.run_command(['make', 'review'])
        self.assertEqual(self.report(first)[0]['status'], 'clear')
        _, directory = self.report(first)
        configuration = directory.parent.parent / 'config.json'
        config = json.loads(configuration.read_text())
        config.update(model='offline/owner', timeout=47, reviewers=3,
                      owner_preference={'keep': 'no Git required'})
        configuration.write_text(json.dumps(config))
        current = self.directory_state(self.rule.parent)
        program = self.source / 'engine/runner/model_common.py'
        program.write_text(program.read_text() + '\nDIRECTORY_UPDATE_FIXTURE = 1\n')
        previous_path = self.environment['PATH']
        self.environment['PATH'] = self.install_path
        try:
            self.commit('new program version for directory review')
        finally:
            self.environment['PATH'] = previous_path
        self.update()
        updated = json.loads(configuration.read_text())
        self.assertNotEqual(updated['engine_digest'], config['engine_digest'])
        self.assertEqual({key: value for key, value in updated.items() if key != 'engine_digest'},
                         {key: value for key, value in config.items() if key != 'engine_digest'})
        self.assertEqual(self.directory_state(self.rule.parent), current)
        self.assertEqual(self.report(self.run_command(['make', 'review']))[0]['status'], 'clear')
        self.assertEqual(json.loads(configuration.read_text()), updated)
        shutil.rmtree(self.rule.parent)
        self.update()
        self.assertEqual(self.directory_state(self.rule.parent), self.directory_state(self.source / 'initial'))
        reset = json.loads(configuration.read_text())
        self.assertTrue(reset['basis_reset'])
        self.assertNotIn('basis_snapshot', reset)
        for name in ('agent', 'agent_command', 'model', 'timeout', 'reviewers', 'owner_preference'):
            self.assertEqual(reset[name], updated[name])
        self.fill_rules()
        self.environment.update(CLARITY_AGENT='pi', CLARITY_AGENT_COMMAND='["must-not-run"]')
        self.assertEqual(self.report(self.run_command(['make', 'review']))[0]['status'], 'clear')
        adopted = json.loads(configuration.read_text())
        self.assertNotIn('basis_reset', adopted)
        self.assertEqual(adopted['basis_snapshot']['.clarity/current/rules.md']['text'], self.rule.read_text())
        for name in ('agent', 'agent_command', 'model', 'timeout', 'reviewers', 'owner_preference'):
            self.assertEqual(adopted[name], updated[name])
        self.assertFalse((self.host / '.git').exists())

    def test_later_git_initialization_and_update_do_not_discard_directory_preferences(self):
        self.fill_rules()
        (self.host / 'identity.py').write_text('def identity(value):\n    return value\n')
        _, directory = self.report(self.run_command(['make', 'review']))
        configuration = directory.parent.parent / 'config.json'
        saved = json.loads(configuration.read_text())
        saved.update(model='offline/owner', owner_preference={'keep': 'created before Git'})
        configuration.write_text(json.dumps(saved))
        previous_path = self.environment['PATH']
        self.environment['PATH'] = self.install_path
        try:
            self.initialize(self.host)
            self.git(self.host, 'add', 'identity.py')
            self.git(self.host, 'commit', '-q', '-m', 'Project chooses Git after directory review')
            index = (self.host / '.git/index').read_bytes()
            self.update()
            completed = self.run_command(['make', 'review'])
        finally:
            self.environment['PATH'] = previous_path
        result, directory = self.report(completed)
        self.assertEqual(result['status'], 'clear')
        current = json.loads((directory.parent.parent / 'config.json').read_text())
        for name in ('agent', 'agent_command', 'model', 'owner_preference', 'basis_snapshot'):
            self.assertEqual(current[name], saved[name])
        self.assertEqual((self.host / '.git/index').read_bytes(), index)

    def test_nested_sidecar_in_unborn_git_uses_git_root_and_nested_current(self):
        parent = self.root / 'unborn parent'
        parent.mkdir()
        self.host = parent / '子 project'
        self.host.mkdir()
        previous_path = self.environment['PATH']
        self.environment['PATH'] = self.install_path
        try:
            self.initialize(parent)
            self.run_command([sys.executable, str(self.source / 'setup.py'), 'install',
                              '--source', str(self.source), '--track', 'no'])
        finally:
            self.environment['PATH'] = previous_path
        (self.binary / 'git').symlink_to(shutil.which('git', path=self.install_path))
        self.rule = self.host / '.clarity/current/rules.md'
        self.fill_rules()
        (self.host / 'identity.py').write_text('def identity(value):\n    return value\n')
        (parent / 'parent_identity.py').write_text('def parent_identity(value):\n    return value\n')
        parent_rules = parent / '.clarity/current/rules.md'
        parent_rules.parent.mkdir(parents=True)
        parent_rules.write_text('PARENT RULES MUST NOT BE ADOPTED.\n')
        parent_config = parent / '.git/clarity/config.json'
        parent_config.parent.mkdir(exist_ok=True)
        parent_config.write_text('{"owner_preference": "parent owner preference survives"}')
        current = self.directory_state(self.rule.parent)
        completed = self.run_command(['make', 'review'])
        result, directory = self.report(completed)
        self.assertEqual(result['status'], 'clear')
        self.assertTrue(directory.is_relative_to(parent / '.git/clarity'))
        source = json.loads((directory / 'input.json').read_text())
        self.assertEqual(source['target_repository']['worktree_root'], str(parent))
        self.assertEqual(set(source['files']), {'parent_identity.py', '子 project/identity.py'})
        configuration = directory.parent.parent / 'config.json'
        config = json.loads(configuration.read_text())
        self.assertEqual(configuration, parent_config)
        self.assertEqual(config['owner_preference'], 'parent owner preference survives')
        nested_basis = '子 project/.clarity/current/rules.md'
        self.assertEqual(config['basis_snapshot'][nested_basis]['text'], self.rule.read_text())
        self.assertNotIn('.clarity/current/rules.md', config['basis_snapshot'])
        config.update(model='offline/owner', timeout=47, reviewers=3)
        configuration.write_text(json.dumps(config))
        response = self.run_command(['make', 'review-respond', 'RESPONSE=Local nested response'], check=False)
        self.assertNotEqual(response.returncode, 0)
        self.assertIn('There is no current finding for a human response', response.stdout + response.stderr)
        self.assertFalse((configuration.parent / 'responses.json').exists())
        self.assertFalse((self.host / '.clarity/.local/responses.json').exists())
        self.assertEqual(json.loads(configuration.read_text()), config)
        program = self.source / 'engine/runner/model_common.py'
        program.write_text(program.read_text() + '\nNESTED_DIRECTORY_UPDATE_FIXTURE = 1\n')
        self.commit('new program version for nested directory review')
        self.update()
        updated = json.loads(configuration.read_text())
        self.assertNotEqual(updated['engine_digest'], config['engine_digest'])
        for name in ('agent', 'agent_command', 'model', 'timeout', 'reviewers', 'owner_preference',
                     'basis_source', 'basis_snapshot', 'basis_worktree'):
            self.assertEqual(updated[name], config[name])
        again = self.run_command(['make', 'review'])
        repeated, repeated_directory = self.report(again)
        self.assertEqual(repeated['status'], 'clear')
        self.assertEqual(repeated_directory.parent.parent, configuration.parent)
        self.assertEqual(json.loads(configuration.read_text()), updated)
        self.assertEqual(self.directory_state(self.rule.parent), current)
        self.assertEqual(updated['owner_preference'], 'parent owner preference survives')
        self.assertEqual(parent_rules.read_text(), 'PARENT RULES MUST NOT BE ADOPTED.\n')
        self.assertFalse((self.host / '.clarity/.local/config.json').exists())
        self.assertFalse((self.host / '.git').exists())
        head = self.run_command(['git', '-C', str(parent), 'rev-parse', '--verify', 'HEAD'], check=False)
        self.assertNotEqual(head.returncode, 0)


if __name__ == '__main__':
    unittest.main()
