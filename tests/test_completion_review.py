import json
from unittest.mock import AsyncMock, patch

from tests import test_direct_core as fixtures
from orchestra_core.models.model_errors import TaskCostLimit
from orchestra_core.runtime.development_route import validate_route
import unittest
from orchestra_core.runtime.task_state import TaskRequest


class CompletionReview(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.DirectCore.setUp
    call = fixtures.DirectCore.call

    async def test_standalone_review_uses_workspace_spec_and_review_budget(self):
        (self.root / 'what_to_do.md').write_text('The program prints ok.', encoding='utf-8')
        (self.root / 'main.py').write_text('print("broken")', encoding='utf-8')
        self.app.options['completion_review_enabled'] = False
        actions = iter([
            self.call('run_command', argv=['{python}', '-c',
                'import subprocess,sys; assert subprocess.check_output([sys.executable,"main.py"]).strip()==b"ok"']),
            self.call('replace_text', path='main.py', old_text='print("broken")', new_text='print("ok")'),
            self.call('run_command', argv=['{python}', 'main.py']),
            self.report(True),
        ])

        async def review_request(role, messages, tools, *args, **kwargs):
            self.assertIn('The program prints ok.', messages[1]['content'])
            self.assertAlmostEqual(self.app.stage_budget()[1], self.app.options['max_task_cost_usd'] * .2)
            self.assertIn('replace_text', {tool['function']['name'] for tool in tools})
            return next(actions)

        self.app.request = AsyncMock(side_effect=review_request)
        result = await self.app.run_user_task(TaskRequest('review', ''), {}, self.trace, self.parent)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['route']['blocks'], ['review'])
        self.assertEqual((self.root / 'main.py').read_text(encoding='utf-8'), 'print("ok")')

    def test_review_git_prompt_matches_available_tools(self):
        definitions = [{'function': {'name': name}} for name in
                       ('git_status', 'git_diff', 'prepare_commit', 'create_commit', 'git_pull', 'git_push')]
        with patch('orchestra_core.runtime.engine.Orchestra.available_tools', return_value=definitions):
            self.app._review_active = True
            names = {tool['function']['name'] for tool in self.app.available_tools()}
            self.assertIn('automatically using git_push', self.app.git_prompt_suffix())
        self.assertNotIn('git_pull', names)
        self.assertTrue({'prepare_commit', 'create_commit', 'git_push'} <= names)
        self.assertTrue({'git_status', 'git_diff', 'run_command'} <= names)

    async def test_baseline_only_report_is_rejected_with_passing_runtime(self):
        self.app.options['completion_review_enabled'] = True
        self.app.settings['execution']['max_steps_per_agent'] = 3
        baseline = self.report(True)
        report = json.loads(baseline['content'])
        report['review']['checks'][0]['kind'] = 'baseline'
        baseline['content'] = json.dumps(report)
        self.app.request = AsyncMock(side_effect=[
            self.call('create_file', path='main.py', content='print("ok")'),
            self.call('run_command', argv=['{python}', 'main.py']), self.report(),
            self.call('run_command', argv=['{python}', 'main.py']), baseline, baseline])
        result = await self.app.run_user_task('Create main.py', {}, self.trace, self.parent)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['completion_review']['status'], 'incomplete')
        self.assertFalse(json.loads(result['answer'])['done'])

    def test_route_validation(self):
        for route in ([], ['review'], ['implement', 'unknown'], ['implement', 'review', 'review']):
            with self.assertRaises(ValueError):
                validate_route(route)
        self.assertEqual(validate_route(['implement', 'review']), ['implement', 'review'])

    async def test_explicit_route_and_budget_interruption(self):
        self.app.options['route'] = ['implement', 'review']
        self.app.request = AsyncMock(side_effect=[
            self.call('create_file', path='main.py', content='print("ok")'),
            self.call('run_command', argv=['{python}', 'main.py']), self.report(),
            TaskCostLimit('Budget exhausted')])
        result = await self.app.run_user_task('Create main.py', {}, self.trace, self.parent)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.app._state['completion_review']['status'], 'interrupted')
        self.assertFalse(self.app._review_active)
        self.assertEqual(self.app._state['route']['steps'][-1]['status'], 'interrupted')

    async def test_partial_implementation_does_not_start_review(self):
        self.app.options['route'] = ['implement', 'review']
        self.app.run = AsyncMock(return_value={'status': 'partial', 'answer': 'Incomplete'})
        result = await self.app.run_user_task('Create main.py', {}, self.trace, self.parent)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.app.run.await_count, 1)
        self.assertNotIn('completion_review', self.app._state)

    def report(self, review=False):
        value = {'summary': 'Checked', 'done': True, 'state': 'complete'}
        if review:
            value['review'] = {'findings': [], 'checks': [
                {'argv': ['{python}', 'main.py'], 'kind': 'independent', 'outcome': 'passed'}],
                'coverage': [{'requirement': 'Requested behavior', 'status': 'verified',
                              'evidence': 'main.py executed successfully'}], 'limitations': []}
        return {'role': 'assistant', 'content': json.dumps(value)}

    async def test_fresh_review_executes_check_and_records_report(self):
        self.app.options['completion_review_enabled'] = True
        self.app.request = AsyncMock(side_effect=[
            self.call('create_file', path='main.py', content='print("ok")'),
            self.call('run_command', argv=['{python}', 'main.py']), self.report(),
            self.call('read_file', path='main.py'),
            self.call('run_command', argv=['{python}', 'main.py']), self.report(True)])
        result = await self.app.run_user_task('Create main.py', {}, self.trace, self.parent)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['completion_review']['status'], 'passed')
        self.assertEqual([step['block'] for step in result['route']['steps']], ['implement', 'review'])
        requests = self.app.request.call_args_list
        self.assertIsNot(requests[0].args[1], requests[3].args[1])
        self.assertIn('recent_edit_diffs', requests[3].args[1][1]['content'])
        self.assertIn('Independently audit', requests[3].args[1][0]['content'])
        self.assertEqual(requests[3].kwargs['max_output_tokens'], 8192)

    async def test_separate_reviewer_uses_its_own_transport_and_usage(self):
        self.app.options['completion_review_enabled'] = True
        self.app.config['providers']['gpt'] = {}
        self.app.config['roles']['reviewer'] = {'provider': 'gpt', 'model': 'gpt-6.1-sol'}
        check = self.call('run_command', argv=['{python}', 'main.py'])

        def transport(replies):
            async def request(app, role, _messages, _tools, _status, usage, _trace, _task_id,
                              *, label, **_kwargs):
                config = app.role_settings(role)
                entry = app.begin_usage(usage, label, role, config['provider'], config['model'])
                entry['completion_tokens'] += 100
                entry['total_tokens'] += 100
                entry['reported_calls'] += 1
                return next(replies)
            return AsyncMock(side_effect=request)

        writer = transport(iter([self.call('create_file', path='main.py', content='print("ok")'),
                                 check, self.report()]))
        reviewer = transport(iter([check, self.report(True)]))
        usage = {}
        with patch('orchestra_core.models.writer_transport.request', writer), patch('orchestra_core.models.responses_transport.request', reviewer):
            result = await self.app.run_user_task('Create main.py', usage, self.trace, self.parent)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(writer.await_count, 3)
        self.assertEqual(reviewer.await_count, 2)
        self.assertEqual(usage['writer']['provider'], 'mimo')
        self.assertEqual(usage['reviewer']['model'], 'gpt-6.1-sol')
        self.assertEqual(usage['writer']['stage'], 'implement')
        self.assertEqual(usage['writer']['files'], ['main.py'])
        self.assertEqual(usage['writer']['lines_added'], 1)
        self.assertEqual(usage['reviewer']['stage'], 'review')
        self.assertEqual(usage['reviewer']['lines_added'], 0)
        self.assertGreater(usage['reviewer']['elapsed_seconds'], 0)
        self.assertAlmostEqual(result['completion_review']['cost_usd'],
                               self.app.estimated_cost(usage['reviewer']))

    async def test_empty_confirmation_cannot_pass_and_calls_are_bounded(self):
        self.app.options['completion_review_enabled'] = True
        self.app.settings['execution']['max_steps_per_agent'] = 3
        self.app.request = AsyncMock(side_effect=[
            self.call('create_file', path='main.py', content='print("ok")'),
            self.call('run_command', argv=['{python}', 'main.py']), self.report(),
            self.report(True), self.report(True), self.report(True)])
        result = await self.app.run_user_task('Create main.py', {}, self.trace, self.parent)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['completion_review']['status'], 'incomplete')
        self.assertEqual(self.app.request.await_count, 6)

    async def test_review_fixes_and_rechecks_defect(self):
        self.app.options['completion_review_enabled'] = True
        self.app.request = AsyncMock(side_effect=[
            self.call('create_file', path='main.py', content='value = 1'),
            self.call('run_command', argv=['{python}', 'main.py']), self.report(),
            self.call('run_command', argv=['{python}', '-c', 'import main; assert main.value == 2']),
            self.call('replace_text', path='main.py', old_text='value = 1', new_text='value = 2'),
            self.call('run_command', argv=['{python}', 'main.py']), self.report(True)])
        result = await self.app.run_user_task('Create main.py with value 2', {}, self.trace, self.parent)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((self.root / 'main.py').read_text(), 'value = 2')
