import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from git_tools import RepositoryTools
from orchestra import Orchestra, validate_settings
from tool_errors import ToolError


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


class RuntimeReliability(unittest.IsolatedAsyncioTestCase):
    def app(self):
        app = Orchestra.__new__(Orchestra)
        app.settings = json.loads(Path('settings.json').read_text())
        app.prompts = json.loads(Path('prompts.json').read_text())
        app.role_settings = MagicMock(return_value={})
        app.dashboard = MagicMock()
        app.repository = SimpleNamespace(available=True, checks={'unit': ['test']},
                                         execute=MagicMock(return_value={'files': ['a.txt'], 'files_changed': 1}))
        app.files = MagicMock()
        app.write_lock = asyncio.Lock()
        app.local_tool_semaphore = asyncio.Semaphore(4)
        return app

    def trace(self):
        trace = MagicMock()
        trace.tasks = {'root': {'depth': 0}}
        trace.new_id.return_value = 'event'
        return trace

    def call(self, name, args):
        return {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}

    async def test_implementation_reserve_is_enforced(self):
        app = self.app()
        scheduler = SimpleNamespace(completion_blockers=lambda: [], submit=MagicMock())
        app.request = AsyncMock(side_effect=[
            {'role': 'assistant', 'content': None, 'tool_calls': [self.call('delegate_task', {'task': 'Inspect', 'max_calls': 4, 'report_target_tokens': 100})]},
            {'role': 'assistant', 'content': None, 'tool_calls': [self.call('report_blocker', {'reason': 'missing_information', 'evidence': 'Relevant source was not provided.', 'next_step': 'Supply the target source.'})]},
            {'role': 'assistant', 'content': 'Insufficient evidence to edit.'}])
        messages = []
        await app.run('director', messages, {'calls': 8}, {}, self.trace(), 'root',
                      max_delegations=1, scheduler=scheduler, mutation_requested=True)
        response = json.loads(next(m['content'] for m in messages if m['role'] == 'tool'))
        self.assertEqual(response['error'], 'batch_worker_budget_exceeded')
        self.assertEqual(response['details']['batch_available_calls'], 3)
        scheduler.submit.assert_not_called()
        self.assertIn('configured checks=["unit"]', app.request.call_args_list[0].args[3])

    async def test_check_requires_previous_patch_result(self):
        app = self.app()
        app.request = AsyncMock(side_effect=[
            {'role': 'assistant', 'content': None, 'tool_calls': [self.call('run_check', {'name': 'unit'}), self.call('apply_patch', {'patch': 'example'})]},
            {'role': 'assistant', 'content': 'Patch applied; check not run.'}])
        messages = []
        await app.run('director', messages, {'calls': 6}, {}, self.trace(), 'root', mutation_requested=True)
        responses = {m['tool_call_id']: json.loads(m['content']) for m in messages if m['role'] == 'tool'}
        self.assertEqual(responses['run_check']['error'], 'check_requires_completed_patch')
        self.assertTrue(responses['apply_patch']['ok'])
        app.repository.execute.assert_called_once_with('apply_patch', {'patch': 'example'})

    async def test_no_checks_hides_tool(self):
        app = self.app()
        app.repository.checks = {}
        app.request = AsyncMock(return_value={'role': 'assistant', 'content': 'Done'})
        await app.run('director', [], {'calls': 3}, {}, self.trace(), 'root')
        names = [tool['function']['name'] for tool in app.request.call_args.args[2]]
        self.assertNotIn('run_check', names)

    def test_invalid_reserve_rejected(self):
        settings = self.app().settings
        settings['execution']['implementation_reserve_calls'] = settings['execution']['max_api_calls_per_turn']
        with self.assertRaisesRegex(ValueError, 'implementation_reserve_calls'):
            validate_settings(settings)


if __name__ == '__main__':
    unittest.main()
