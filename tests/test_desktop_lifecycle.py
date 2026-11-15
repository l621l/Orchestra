import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra_app import OrchestraApp


class DesktopLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_and_resume_in_new_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            base = Path(__file__).resolve().parents[1]
            from direct_dev import DirectDevelopment

            def open_session():
                config = {'providers': {'mimo': {}, 'gpt': {}}, 'roles': {
                    'writer': {'provider': 'mimo', 'model': 'mimo-v2.6-pro'},
                    'strong': {'provider': 'gpt', 'model': 'gpt-6.1-sol'},
                }}
                import json

                settings, prompts, pricing = [
                    json.loads((base / name).read_text(encoding='utf-8'))
                    for name in ('settings.json', 'prompts.json', 'pricing.json')
                ]
                return OrchestraApp(DirectDevelopment(config, folder, settings, prompts, pricing,
                                                        logs_path=folder / 'logs'))

            first = open_session()
            started = asyncio.Event()

            async def pending(*args, **kwargs):
                started.set()
                await asyncio.Future()

            with patch('writer_transport.request', side_effect=pending):
                task = asyncio.create_task(first.new_task('Make a small app'))
                await asyncio.wait_for(started.wait(), timeout=5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            second = open_session()
            self.assertEqual(second.status()['stop_reason'], 'cancelled')
            first.engine._state['budget_spent'] = first.engine.options['max_task_cost_usd'] + 1
            first.engine.save_state()
            with patch('writer_transport.request', side_effect=[
                {'role': 'assistant', 'tool_calls': [{
                    'id': 'edit', 'type': 'function', 'function': {
                        'name': 'create_file',
                        'arguments': '{"path":"feature.txt","content":"Ready"}',
                    },
                }]},
                {'role': 'assistant',
                 'content': '{"summary":"Saved","next_steps":"","done":true,"state":"complete"}'},
                {'role': 'assistant',
                 'content': '{"summary":"Reviewed saved file against the request","next_steps":"","done":true,"state":"complete","review":{"risks":[{"scenario":"Wrong saved text","evidence":"feature.txt contains Ready","basis":"code","status":"excluded"}],"gaps":[]}}'},
            ]):
                result = await second.continue_task()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual((folder / 'feature.txt').read_text(), 'Ready')
            self.assertEqual(second.engine._state['specification'], 'Make a small app')
            self.assertEqual(second.engine._state['budget_spent'], 0.0)

