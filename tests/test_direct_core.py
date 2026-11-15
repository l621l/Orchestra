import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from rich.console import Console

from model_errors import TaskCostLimit
from model_routing import role_settings
from orchestra_config import normalize_settings
from orchestra_console import AgentDashboard
from tests import test_direct_dev as fixtures


class DirectCore(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.DirectLoop.setUp
    call = fixtures.DirectLoop.call

    def test_direct_validates_model_transport(self):
        for model, transport in [('gpt-6.1-sol', 'chat'),
                                 ('mimo-v2.6-pro', 'responses')]:
            config = copy.deepcopy(self.app.config)
            config['roles']['writer'].update(model=model, transport=transport)
            with self.assertRaises(ValueError):
                role_settings(config, 'writer')
        with self.assertRaises(ValueError):
            self.app.role_settings('strong')
        names = {tool['function']['name'] for tool in self.app.available_tools()}
        self.assertFalse(names & {'ask_expert', 'set_plan', 'finish_task', 'compact_context'})

    async def test_gpt_direct_runs_tool_and_returns_output_to_responses(self):
        self.app.config['providers']['gpt'] = {}
        self.app.config['roles']['writer'] = {'provider': 'gpt', 'model': 'gpt-6.1-sol'}
        item = SimpleNamespace(model_dump=lambda **kwargs: {
            'type': 'function_call', 'call_id': 'edit', 'status': 'completed',
            'name': 'create_file', 'arguments': '{"path":"result.txt","content":"ok"}',
        })
        async def events(response, summary=None):
            if summary:
                yield SimpleNamespace(type='response.reasoning_summary_text.delta', delta=summary)
            yield SimpleNamespace(type='response.completed', response=response)

        first = AsyncMock()
        first.__aenter__.return_value = events(
            SimpleNamespace(id='first', model='gpt-6.1-sol', usage=None,
                            status='completed', output=[item], output_text=''),
            'Проверяю структуру проекта')
        summary_item = SimpleNamespace(
            type='reasoning',
            summary=[SimpleNamespace(text='Проверка завершена')],
            model_dump=lambda **kwargs: {'type': 'reasoning', 'summary': []},
        )
        last = AsyncMock()
        last.__aenter__.return_value = events(
            SimpleNamespace(id='final', model='gpt-6.1-sol', usage=None,
                            status='completed', output=[summary_item], output_text=json.dumps({
                                'summary': 'Created', 'done': True, 'state': 'complete',
                            })))
        create = AsyncMock(side_effect=[first, last])
        progress = []
        self.trace.on_progress = progress.append
        self.app.clients['gpt'] = SimpleNamespace(responses=SimpleNamespace(create=create))
        result = await self.app.run_user_task('Create result.txt', {}, self.trace, self.parent)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((self.root / 'result.txt').read_text(), 'ok')
        self.assertTrue(create.call_args.kwargs['stream'])
        self.assertEqual(create.call_args.kwargs['reasoning']['summary'], 'auto')
        self.assertEqual([event['text'] for event in progress if event['kind'] == 'reasoning'],
                         ['Проверяю структуру проекта', 'Проверка завершена'])
        self.assertTrue(any(item.get('type') == 'function_call_output' and item['call_id'] == 'edit'
                            for item in create.call_args.kwargs['input']))

    def test_legacy_settings_keep_limits_without_reenabling_old_modes(self):
        legacy = copy.deepcopy(self.app.settings)
        legacy['pipeline'] = legacy.pop('direct')
        legacy['pipeline'].update(strong_calls=10, strong_request_characters=16000)
        legacy['execution']['completion_review_enabled'] = True
        normalized = normalize_settings(legacy)
        self.assertEqual(normalized['direct']['max_task_cost_usd'],
                         self.app.options['max_task_cost_usd'])
        self.assertNotIn('pipeline', normalized)
        self.assertNotIn('strong_calls', normalized['direct'])
        self.assertNotIn('completion_review_enabled', normalized['execution'])
        self.assertIn('pipeline', legacy)

    async def test_forced_edit_then_check_and_completion_without_reviewer(self):
        self.app.settings['execution']['research_calls_before_action'] = 1
        self.app.request = AsyncMock(side_effect=[
            self.call('list_files', path='.'),
            self.call('create_file', path='main.py', content='print("works")'),
            self.call('run_command', argv=['{python}', 'main.py']),
            {'role': 'assistant', 'content': json.dumps({
                'summary': 'Implemented and checked', 'done': True, 'state': 'complete'})},
        ])
        result = await self.app.run_user_task('Create main.py', {}, self.trace, self.parent)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.app.request.await_count, 4)
        requests = self.app.request.call_args_list
        self.assertEqual({t['function']['name'] for t in requests[1].args[2]},
                         {'create_file', 'replace_text'})
        self.assertIn('run_command', {t['function']['name'] for t in requests[2].args[2]})
        self.assertFalse(any('review' in e['event'] for e in self.trace.events))

    async def test_cost_stop_prevents_next_request_and_resume_retains_task(self):
        self.app.load_or_start('Create program')
        self.app._state['budget_spent'] = self.app.options['max_task_cost_usd'] + 1
        self.app.save_state()
        with patch('writer_transport.request', new_callable=AsyncMock) as request:
            with self.assertRaises(TaskCostLimit):
                await self.app.request('worker', [], [], '', {}, self.trace, self.parent)
            request.assert_not_called()
        self.app.load_or_start('C')
        self.assertEqual(self.app._state['specification'], 'Create program')
        self.assertGreater(self.app._state['budget_spent'], 0)

    def test_single_model_dashboard_with_no_turn_token_cap(self):
        dashboard = AgentDashboard()
        output = io.StringIO()
        dashboard.console = Console(file=output, width=160, color_system=None)
        dashboard.configure_models({'writer': 'mimo-v2.6-pro'})
        dashboard.activate_model('writer')
        dashboard.update_output_budget(1234, None)
        dashboard.console.print(dashboard.render())
        self.assertEqual(set(dashboard.states), {'writer'})
        self.assertIn('mimo-v2.6-pro', output.getvalue())
        self.assertNotIn('Next:', output.getvalue())
