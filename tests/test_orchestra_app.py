import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from orchestra_app import OrchestraApp
from orchestra_progress import SilentProgress
from project_usage import IGNORE_RULE, read_usage
from workspace_lease import WorkspaceBusy


class ApplicationTurns(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = SimpleNamespace(
            files=SimpleNamespace(root=self.root),
            logs_path=self.root,
            state_path=self.root / 'checkpoint.json',
            total_output_limit=20000,
            dashboard=SilentProgress(),
            run_user_task=AsyncMock(return_value={
                'status': 'completed', 'answer': 'Done', 'modified_files': [],
                'verification': {'status': 'not_run'},
            }),
        )
        self.app = OrchestraApp(self.engine)

    async def test_run_returns_result_and_streams_independent_events(self):
        events = []

        def receive(event):
            events.append(event.copy())
            if event['event'] == 'workspace_result':
                event['verification']['status'] = 'changed by frontend'

        self.assertEqual(self.app.status(), {'status': 'new', 'running': False})
        result = await self.app.run('Implement a feature', on_event=receive)
        self.assertEqual(result['verification']['status'], 'not_run')
        self.assertEqual(result['usage'], {})
        self.assertEqual(events[-1]['event'], 'turn_finished')
        self.assertTrue(all(event['turn_id'] == result['turn_id'] for event in events))
        log = Path(result['log_directory'])
        self.assertEqual((log / 'answer.md').read_text(encoding='utf-8'), 'Done\n')
        self.assertEqual(json.loads((log / 'summary.json').read_text(encoding='utf-8'))['status'],
                         'completed')

    async def test_completed_turn_persists_final_cost_and_publishes_it(self):
        result_body = self.engine.run_user_task.return_value

        async def run(text, usage, trace, task_id):
            usage['writer'] = {'api_calls': 2, 'reported_calls': 2}
            return result_body

        self.engine.run_user_task.side_effect = run
        self.engine.estimated_cost = Mock(return_value=0.4344671)
        events = []
        result = await self.app.new_task('Implement a feature', on_progress=events.append)
        path = Path(result['log_directory']) / 'summary.json'
        summary = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(summary['status'], 'completed')
        self.assertEqual(summary['cost'], {'estimated_usd': 0.4344671, 'complete': True})
        self.assertEqual(events[-1]['cost'], summary['cost'])

    async def test_project_usage_survives_new_sessions_and_counts_cancelled_calls(self):
        started = asyncio.Event()
        result_body = self.engine.run_user_task.return_value

        async def run(text, usage, trace, task_id):
            model = 'sol' if not started.is_set() else 'other'
            usage['writer'] = {'provider': 'gpt', 'model': model, 'api_calls': 1,
                               'reported_calls': 1, 'total_tokens': 55, 'cached_tokens': 30}
            if started.is_set():
                await asyncio.Future()
            started.set()
            return result_body

        self.engine.run_user_task.side_effect = run
        self.engine.estimated_cost = Mock(side_effect=[0.25, 0.125])
        ignore = self.root / '.gitignore'
        ignore.write_bytes(b'/private')
        await self.app.new_task('Implement a feature')
        second = asyncio.create_task(OrchestraApp(self.engine).new_task('Another feature'))
        await asyncio.sleep(0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        data = read_usage(self.root)
        self.assertEqual(data['runs'], 2)
        self.assertEqual(data['total']['estimated_usd'], 0.375)
        self.assertEqual(data['total']['cached_tokens'], 60)
        self.assertEqual({entry['model']: entry['estimated_usd'] for entry in data['models']},
                         {'sol': 0.25, 'other': 0.125})
        self.assertEqual(ignore.read_text(encoding='utf-8').splitlines(), ['/private', IGNORE_RULE])

    async def test_cancel_closes_trace_and_releases_session(self):
        started = asyncio.Event()

        async def wait_for_cancellation(*args):
            started.set()
            await asyncio.Future()

        self.engine.run_user_task.side_effect = wait_for_cancellation
        events = []
        turn = asyncio.create_task(self.app.run('Implement a feature', on_event=events.append))
        await started.wait()
        self.assertTrue(self.app.status()['running'])
        second_session = OrchestraApp(self.engine)
        self.assertTrue(second_session.status()['running'])
        with self.assertRaises(WorkspaceBusy):
            await second_session.new_task('Another feature')
        with self.assertRaisesRegex(RuntimeError, 'already has a running turn'):
            await self.app.run('Another feature')
        turn.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await turn
        self.assertFalse(self.app.status()['running'])
        self.assertEqual(events[-1]['status'], 'cancelled')
        summary, = self.root.glob('*/*/summary.json')
        self.assertEqual(json.loads(summary.read_text(encoding='utf-8'))['status'], 'cancelled')
        self.engine.run_user_task.side_effect = None
        self.assertEqual((await self.app.run('Continue'))['status'], 'completed')
    async def test_explicit_action_preserves_literal_text(self):
        progress = []
        result = await self.app.new_task('D: a literal title', on_progress=progress.append)
        request = self.engine.run_user_task.await_args.args[0]
        self.assertEqual((request.action, request.text), ('new', 'D: a literal title'))
        self.assertEqual([event['kind'] for event in progress], ['started', 'stopped'])
        self.assertEqual(progress[-1]['status'], result['status'])

    async def test_saved_running_status_without_lease_is_interrupted(self):
        self.engine.options = {'max_task_cost_usd': 0.1}
        self.engine.state_path.write_text(json.dumps({
            'version': 2, 'workspace': str(self.root), 'status': 'running', 'plan': [],
            'mode': 'direct',
        }), encoding='utf-8')
        self.assertEqual(self.app.status()['stop_reason'], 'interrupted')
        self.assertFalse(self.app.status()['running'])
