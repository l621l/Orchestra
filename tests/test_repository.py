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

class RepositoryReliability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='orchestra-git-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def write(self, name, value):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value, encoding='utf-8')

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'test snapshot')

    def test_push_publishes_current_branch_and_sets_upstream(self):
        self.write('file.txt', 'committed content')
        self.commit()
        self.git('checkout', '-b', 'publish-test')
        with tempfile.TemporaryDirectory() as remote:
            subprocess.run(['git', 'init', '-q', '--bare', remote], check=True)
            self.git('remote', 'add', 'origin', remote)
            repo = RepositoryTools(self.root)
            self.assertTrue(repo.execute('git_push', {})['pushed'])
            self.assertEqual(self.git('rev-parse', '@{upstream}'), self.git('rev-parse', 'HEAD'))
            self.assertTrue(repo.execute('git_push', {})['pushed'])
            self.assertEqual(self.git('ls-remote', '--heads', 'origin').splitlines(),
                             [self.git('rev-parse', 'HEAD').strip() + '\trefs/heads/publish-test'])

    def test_push_preflight_defers_until_first_commit(self):
        repo = RepositoryTools(self.root)
        with self.assertRaises(ToolError) as missing:
            repo.check_push()
        self.assertEqual(missing.exception.code, 'push_remote_missing')
        with tempfile.TemporaryDirectory() as remote:
            subprocess.run(['git', 'init', '-q', '--bare', remote], check=True)
            self.git('remote', 'add', 'origin', remote)
            repo.check_push()
            self.write('file.txt', 'first change')
            self.commit()
            repo.check_push()
            self.assertTrue(repo.git_push()['pushed'])

    def test_pull_updates_from_upstream_and_rejects_dirty_worktree(self):
        self.write('file.txt', 'before')
        self.commit()
        branch = self.git('symbolic-ref', '--short', 'HEAD').strip()
        with tempfile.TemporaryDirectory() as upstream:
            subprocess.run(['git', 'clone', '-q', str(self.root), upstream], check=True)
            self.git('remote', 'add', 'origin', upstream)
            self.git('fetch', '-q', 'origin')
            self.git('branch', '--set-upstream-to', f'origin/{branch}')
            (Path(upstream) / 'file.txt').write_text('after', encoding='utf-8')
            subprocess.run(['git', '-C', upstream, '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.invalid', 'commit', '-qam', 'update'], check=True)
            repo = RepositoryTools(self.root)
            self.write('local.txt', 'local changes')
            with self.assertRaisesRegex(ToolError, 'Save local changes'):
                repo.execute('git_pull', {})
            (self.root / 'local.txt').unlink()
            self.assertTrue(repo.execute('git_pull', {})['updated'])
            self.assertEqual((self.root / 'file.txt').read_text(), 'after')
            self.assertFalse(repo.execute('git_pull', {})['updated'])

    def test_history_and_diff_filter_restricted_files(self):
        self.write('src/a.txt', 'public old\n')
        self.write('src/config.local.json', 'DUMMY_SECRET_OLD\n')
        self.commit()
        repo = RepositoryTools(self.root)
        self.assertNotIn('DUMMY_SECRET', repo.git_show('HEAD')['show'])
        self.write('src/a.txt', 'public new\n')
        self.write('src/config.local.json', 'DUMMY_SECRET_NEW\n')
        for path in (None, '.', 'src'):
            result = repo.git_diff(path=path)['diff']
            self.assertIn('public new', result)
            self.assertNotIn('DUMMY_SECRET', result)
        self.commit()
        for path in (None, '.', 'src', 'src/a.txt'):
            result = repo.git_show('HEAD', path=path)['show']
            self.assertIn('public new', result)
            self.assertNotIn('DUMMY_SECRET', result)

    def test_historical_deleted_files_and_no_allowed_files(self):
        self.write('public.txt', 'visible\n')
        self.write('config.local.json', 'DUMMY_SECRET\n')
        self.commit()
        self.git('rm', 'public.txt')
        self.git('commit', '-qm', 'delete public file')
        repo = RepositoryTools(self.root)
        self.assertIn('-visible', repo.git_show('HEAD')['show'])
        self.write('config.local.json', 'DUMMY_SECRET_CHANGED\n')
        self.commit()
        self.assertNotIn('DUMMY_SECRET', repo.git_show('HEAD')['show'])

    def test_post_patch_inspection_failure_preserves_success(self):
        self.write('a.txt', 'old\n')
        self.commit()
        repo = RepositoryTools(self.root)
        real_run = repo._run
        def run(args, **kwargs):
            if args[0] == 'diff':
                raise ToolError('git_failed', 'simulated')
            return real_run(args, **kwargs)
        diff = 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n'
        with patch.object(repo, '_run', side_effect=run), patch.object(repo, 'git_status', side_effect=ToolError('git_failed', 'simulated')):
            result = repo.apply_patch(diff)
        self.assertTrue(result['applied'])
        self.assertEqual(len(result['warnings']), 2)
        self.assertEqual((self.root/'a.txt').read_text(), 'new\n')
