"""Regression checks for commit tools alongside restored workspace tools."""
import subprocess
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_tools import RepositoryTools
from tool_errors import ToolError
from tests import test_base_tools


class CommitTools(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='orchestra-commit-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.repo = RepositoryTools(self.root)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def baseline(self):
        (self.root / 'a.txt').write_text('base\n')
        self.git('add', 'a.txt')
        self.git('commit', '-qm', 'baseline')
        (self.root / 'a.txt').write_text('changed\n')

    def commit(self, snapshot, paths=None):
        return self.repo.execute('create_commit', {
            'message': 'fix: change a', 'paths': paths or ['a.txt'],
            'fingerprint': snapshot['fingerprint']})

    def test_restricted_staged_file_blocks_commit_and_preserves_index(self):
        self.baseline()
        (self.root / 'config.local.json').write_text('DUMMY_SECRET')
        self.git('add', 'config.local.json')
        before = self.git('diff', '--cached', '--binary')
        head = self.git('rev-parse', 'HEAD')
        snapshot = self.repo.execute('prepare_commit', {})
        self.assertTrue(snapshot['staged_changes_present'])
        self.assertNotIn('config.local.json', snapshot['paths'])
        self.assertNotIn('DUMMY_SECRET', snapshot['diff'])
        with self.assertRaises(ToolError) as caught:
            self.commit(snapshot)
        self.assertEqual(caught.exception.code, 'staged_changes_present')
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.git('diff', '--cached', '--binary'), before)

    def test_new_repository_can_commit_and_retry_is_stale(self):
        self.repo.execute('create_file', {'path': 'a.txt', 'content': 'new\n'})
        snapshot = self.repo.prepare_commit()
        result = self.commit(snapshot)
        self.assertTrue(result['committed'])
        self.assertEqual(self.git('show', 'HEAD:a.txt'), 'new')
        with self.assertRaises(ToolError):
            self.commit(snapshot)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '1')

    def test_snapshot_changes_with_branch_even_at_same_head(self):
        self.baseline()
        snapshot = self.repo.prepare_commit()
        self.git('checkout', '-qb', 'another')
        with self.assertRaises(ToolError) as caught:
            self.commit(snapshot)
        self.assertEqual(caught.exception.code, 'stale_commit_plan')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_snapshot_changes_with_untracked_contents(self):
        (self.root / 'a.txt').write_text('before')
        snapshot = self.repo.prepare_commit()
        (self.root / 'a.txt').write_text('after')
        with self.assertRaises(ToolError) as caught:
            self.commit(snapshot)
        self.assertEqual(caught.exception.code, 'stale_commit_plan')

    def test_failed_commit_unstages_without_reverting_worktree(self):
        for initial in (True, False):
            with self.subTest(initial=initial):
                if not initial:
                    self.baseline()
                else:
                    (self.root / 'a.txt').write_text('changed\n')
                snapshot = self.repo.prepare_commit()
                original = self.repo._run

                def fail_commit(args, *rest, **kwargs):
                    if args[0] == 'commit':
                        raise ToolError('git_failed', 'simulated commit rejection')
                    return original(args, *rest, **kwargs)

                with patch.object(self.repo, '_run', side_effect=fail_commit):
                    with self.assertRaises(ToolError):
                        self.commit(snapshot)
                self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
                self.assertEqual((self.root / 'a.txt').read_text(), 'changed\n')

    def test_successful_commit_survives_inspection_failure(self):
        self.baseline()
        snapshot = self.repo.prepare_commit()
        with patch.object(self.repo, 'git_status', side_effect=ToolError('git_failed', 'simulated')):
            result = self.commit(snapshot)
        self.assertTrue(result['committed'])
        self.assertTrue(result['warnings'])
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')

    def test_create_branch_in_unborn_repository_preserves_files(self):
        (self.root / 'a.txt').write_text('uncommitted')
        result = self.repo.execute('git_create_branch', {'name': 'feat/first-project'})
        self.assertTrue(result['created'])
        self.assertEqual(self.git('branch', '--show-current'), 'feat/first-project')
        self.assertEqual((self.root / 'a.txt').read_text(), 'uncommitted')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_branch_creation_preserves_head_and_refuses_existing_branch(self):
        self.baseline()
        head = self.git('rev-parse', 'HEAD')
        self.repo.execute('git_create_branch', {'name': 'feat/new-work'})
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual((self.root / 'a.txt').read_text(), 'changed\n')
        with self.assertRaises(ToolError) as error:
            self.repo.execute('git_create_branch', {'name': 'feat/new-work'})
        self.assertEqual(error.exception.code, 'branch_exists')

    def test_invalid_branch_names_make_no_changes(self):
        original = self.git('symbolic-ref', 'HEAD')
        for name in ('-f', 'HEAD', 'a..b', 'a@{1}', 'a b', 'a.lock', '../x', 'a/', ''):
            with self.subTest(name=name), self.assertRaises(ToolError):
                self.repo.execute('git_create_branch', {'name': name})
            self.assertEqual(self.git('symbolic-ref', 'HEAD'), original)

    def test_branch_refuses_staged_changes_without_touching_index(self):
        (self.root / 'a.txt').write_text('staged')
        self.git('add', 'a.txt')
        original = self.git('symbolic-ref', 'HEAD')
        index = self.git('diff', '--cached', '--binary')
        with self.assertRaises(ToolError) as error:
            self.repo.git_create_branch('feat/new')
        self.assertEqual(error.exception.code, 'staged_changes_present')
        self.assertEqual(self.git('symbolic-ref', 'HEAD'), original)
        self.assertEqual(self.git('diff', '--cached', '--binary'), index)

    def test_created_branch_survives_inspection_failure(self):
        with patch.object(self.repo, 'git_status', side_effect=ToolError('git_failed', 'simulated')):
            result = self.repo.git_create_branch('feat/new')
        self.assertTrue(result['created'])
        self.assertTrue(result['warnings'])
        self.assertEqual(self.git('branch', '--show-current'), 'feat/new')

    def test_missing_identity_is_reported_before_staging(self):
        self.baseline()
        snapshot = self.repo.prepare_commit()
        original = self.repo._run

        def missing_identity(args, **kwargs):
            if args[0] == 'var':
                return {'returncode': 128, 'stdout': '', 'stderr': 'identity missing'}
            return original(args, **kwargs)

        with patch.object(self.repo, '_run', side_effect=missing_identity), self.assertRaises(ToolError) as error:
            self.commit(snapshot)
        self.assertEqual(error.exception.code, 'git_identity_missing')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        self.assertEqual((self.root / 'a.txt').read_text(), 'changed\n')


class CommitRuntime(unittest.IsolatedAsyncioTestCase):
    # Reuse only the harness; the base tests are discovered in their own module.
    setUp = test_base_tools.RuntimeContracts.setUp
    call = test_base_tools.RuntimeContracts.call
    response = test_base_tools.RuntimeContracts.response
    run_responses = test_base_tools.RuntimeContracts.run_responses

    def enable_autonomous_git(self):
        self.app.repository._run(['init', '-q'])
        repo = RepositoryTools(self.root, checks=self.app.repository.checks)
        repo._run(['config', 'user.name', 'Test'])
        repo._run(['config', 'user.email', 'test@example.invalid'])
        self.app.repository = repo
        self.app.settings['git'] = {'commit_mode': 'autonomous', 'allow_branch_creation': True}
        self.app.settings['execution']['workflow'] = 'project'
        return repo

    async def test_autonomous_branch_edit_check_commit_end_to_end(self):
        repo = self.enable_autonomous_git()
        responses = iter([
            self.response('git_create_branch', {'name': 'feat/test-project'}),
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2\n'}),
            self.response('run_check', {'name': 'unit'}),
            self.response('prepare_commit', {}),
            None,
            {'role': 'assistant', 'content': 'Implemented, checked and committed.'}])

        async def respond(*args, **kwargs):
            response = next(responses)
            if response is None:
                return self.response('create_commit', {'message': 'feat: implement value', 'paths': ['new.py'],
                    'fingerprint': repo.prepare_commit()['fingerprint']})
            return response

        from unittest.mock import AsyncMock
        self.app.request = AsyncMock(side_effect=respond)
        messages = []
        result = await self.app.run('director', messages, {'calls': 16}, {}, self.trace, self.task,
                                    mutation_requested=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(repo._run(['branch', '--show-current'])['stdout'].strip(), 'feat/test-project')
        self.assertEqual(repo._run(['show', 'HEAD:new.py'])['stdout'], 'value = 2\n')
        self.assertEqual(repo._run(['status', '--porcelain', '--untracked-files=no'])['stdout'], '')
        events = [e for e in self.trace.events if e['event'] == 'tool_finished']
        self.assertTrue(all(e['ok'] for e in events))
        self.assertEqual(events[0]['result']['branch'], 'feat/test-project')
        self.assertEqual(events[-1]['result']['message'], 'feat: implement value')
        self.assertEqual(self.trace.tasks[self.task]['metrics']['git_calls'], 3)

    async def test_autonomous_commit_requires_every_current_check(self):
        repo = self.enable_autonomous_git()
        (self.root / 'new.py').write_text('value = 2\n')
        repo.checks['other'] = [sys.executable, '-B', '-c', 'assert True']
        args = {'message': 'feat: add value', 'paths': ['new.py'], 'fingerprint': repo.prepare_commit()['fingerprint']}
        result, messages = await self.run_responses([
            self.response('create_commit', args),
            self.response('run_check', {'name': 'unit'}),
            self.response('create_commit', args),
            self.response('run_check', {'name': 'other'}),
            self.response('create_commit', args),
            {'role': 'assistant', 'content': 'Committed after both checks.'}])
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        self.assertEqual([o['error'] for o in outputs if not o['ok']], ['commit_requires_checks'] * 2)
        self.assertTrue(outputs[-1]['result']['committed'])
        self.assertEqual(repo._run(['rev-list', '--count', 'HEAD'])['stdout'].strip(), '1')

    async def test_edit_invalidates_autonomous_commit_checks(self):
        repo = self.enable_autonomous_git()
        (self.root / 'new.py').write_text('value = 2\n')
        _, messages = await self.run_responses([
            self.response('run_check', {'name': 'unit'}),
            self.response('replace_text', {'path': 'new.py', 'old_text': '2', 'new_text': '3'}),
            self.response('create_commit', {'message': 'bad', 'paths': ['new.py'], 'fingerprint': '0' * 64}),
            {'role': 'assistant', 'content': 'Changes need rechecking.'}])
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        self.assertEqual(outputs[-1]['error'], 'commit_requires_checks')
        self.assertNotEqual(repo._run(['rev-parse', '--verify', 'HEAD'], check=False)['returncode'], 0)

    async def test_disabled_git_mutations_are_not_exposed_or_dispatched(self):
        repo = self.enable_autonomous_git()
        self.app.settings['git'] = {'commit_mode': 'disabled', 'allow_branch_creation': False}
        _, messages = await self.run_responses([
            self.response('git_create_branch', {'name': 'feat/forbidden'}),
            self.response('create_commit', {'message': 'bad', 'paths': ['new.py'], 'fingerprint': '0' * 64}),
            {'role': 'assistant', 'content': 'Disabled.'}])
        exposed = {t['function']['name'] for t in self.app.request.call_args_list[0].args[2]}
        self.assertNotIn('git_create_branch', exposed)
        self.assertNotIn('create_commit', exposed)
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        self.assertTrue(all(o['error'] == 'tool_unavailable' for o in outputs))

    async def test_branch_mixed_with_edit_performs_neither(self):
        repo = self.enable_autonomous_git()
        branch = repo._run(['symbolic-ref', 'HEAD'])['stdout']
        _, messages = await self.run_responses([
            {'role': 'assistant', 'content': None, 'tool_calls': [
                self.call('git_create_branch', {'name': 'feat/conflict'}),
                self.call('create_file', {'path': 'new.py', 'content': 'value = 2'})]},
            {'role': 'assistant', 'content': 'Retry separately.'}])
        self.assertFalse((self.root / 'new.py').exists())
        self.assertEqual(repo._run(['symbolic-ref', 'HEAD'])['stdout'], branch)
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        self.assertTrue(all(o['error'] == 'conflicting_actions' for o in outputs))

    async def test_worker_has_no_git_mutation_tools_even_in_autonomous_mode(self):
        repo = self.enable_autonomous_git()
        branch = repo._run(['symbolic-ref', 'HEAD'])['stdout']
        _, messages = await self.run_responses([
            self.response('git_create_branch', {'name': 'feat/forbidden'}),
            self.response('create_commit', {'message': 'bad', 'paths': ['new.py'], 'fingerprint': '0' * 64}),
            {'role': 'assistant', 'content': 'Read-only.'},
            {'role': 'assistant', 'content': 'Read-only.'}], role='worker')
        exposed = {t['function']['name'] for t in self.app.request.call_args_list[0].args[2]}
        self.assertNotIn('git_create_branch', exposed)
        self.assertNotIn('create_commit', exposed)
        self.assertEqual(repo._run(['symbolic-ref', 'HEAD'])['stdout'], branch)
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        self.assertTrue(all(o['error'] == 'tool_unavailable' for o in outputs))

    async def test_commit_dispatch_trace_and_workspace_edits_coexist(self):
        repo = self.app.repository
        repo._run(['init', '-q'])
        repo = RepositoryTools(self.root)
        repo._run(['config', 'user.name', 'Test'])
        repo._run(['config', 'user.email', 'test@example.invalid'])
        self.app.repository = repo
        (self.root / 'a.txt').write_text('new\n')
        snapshot = repo.prepare_commit()
        result, _ = await self.run_responses([
            self.response('prepare_commit', {}),
            self.response('create_commit', {'message': 'feat: add a', 'paths': ['a.txt'],
                                           'fingerprint': snapshot['fingerprint']}),
            {'role': 'assistant', 'content': 'Committed.'}])
        self.assertEqual(result['status'], 'completed')
        events = [e for e in self.trace.events if e['event'] == 'tool_finished']
        self.assertTrue(all(e['ok'] for e in events))
        self.assertTrue(events[-1]['result']['committed'])
        self.assertEqual(self.trace.tasks[self.task]['metrics']['git_calls'], 2)

    async def test_commit_and_edit_in_same_response_perform_neither(self):
        self.app.repository.available = True
        result, messages = await self.run_responses([
            {'role': 'assistant', 'content': None, 'tool_calls': [
                self.call('create_file', {'path': 'new.py', 'content': 'x=1'}),
                self.call('create_commit', {'message': 'test', 'paths': ['new.py'], 'fingerprint': '0'*64})]},
            {'role': 'assistant', 'content': 'Unable to commit.'}])
        self.assertFalse((self.root / 'new.py').exists())
        events = [e for e in self.trace.events if e['event'] == 'tool_finished']
        self.assertEqual([e['error_type'] for e in events], ['conflicting_actions'] * 2)
