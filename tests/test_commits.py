import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from orchestra_core.workspace.git_tools import RepositoryTools
from orchestra_core.workspace.tool_errors import ToolError

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
