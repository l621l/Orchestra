import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from rich.console import Console
from orchestra import AgentDashboard, ModelResponseError, error_description
from tests import test_single_model as fixtures


class ConsoleFeedback(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.dashboard = AgentDashboard()
        self.dashboard.console = Console(file=self.stream, width=160, color_system=None)

    def test_actions_include_path_result_and_duration_without_markup(self):
        self.dashboard.tool_finished('director', 'read_file', {'path':'[bold]sample.py'},
            {'ok':True, 'result':{'returned_start_line':1,'returned_end_line':12}}, .25)
        output = self.stream.getvalue()
        self.assertIn('[bold]sample.py', output)
        self.assertIn('lines 1-12', output)
        self.assertIn('0.25s', output)
        self.assertNotIn('\x1b', output)

    def test_failed_checks_and_tool_errors_show_actual_diagnostics(self):
        self.dashboard.tool_finished('director','run_check',{'name':'unit'},
            {'ok':True,'result':{'passed':False,'returncode':1,'output':'Traceback\nAssertionError: wrong value'}},.5)
        self.dashboard.tool_finished('director','replace_text',{'path':'a.py'},
            {'ok':False,'error':'text_not_found','message':'Read the current fragment.'},.1)
        output = self.stream.getvalue()
        self.assertIn('FAIL (exit=1)', output)
        self.assertIn('AssertionError: wrong value', output)
        self.assertIn('text_not_found', output)
        self.assertIn('Read the current fragment.', output)

    def test_provider_error_body_is_not_printed(self):
        error = RuntimeError('secret api key and provider body')
        error.status_code = 401
        description = error_description(error)
        self.assertIn('HTTP 401', description)
        self.assertIn('authentication', description)
        self.assertNotIn('secret', description)


class RequestDiagnostics(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SingleModel.setUp

    async def test_reasoning_only_length_error_explains_and_persists_failure(self):
        chunk = SimpleNamespace(id='offline-id', model='offline', usage=None,
            choices=[SimpleNamespace(finish_reason='length', delta=SimpleNamespace(
                content=None, reasoning_content='private reasoning', tool_calls=None))])
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def __aiter__(self):
                async def generate():
                    yield chunk
                return generate()
        self.app.clients['offline'] = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=Stream()))))
        usage = {}
        with self.assertRaises(ModelResponseError) as caught:
            await self.app.request('director', [], [], '', usage, self.trace, self.task)
        self.assertIn('Output token limit reached', str(caught.exception))
        self.assertIn(str(self.settings['generation']['max_completion_tokens']), str(caught.exception))
        self.assertEqual(usage['director']['output_budget_tokens'],self.settings['generation']['max_completion_tokens'])
        self.assertIn('visible answer=0', str(caught.exception))
        self.trace.finish('failed', type(caught.exception).__name__)
        summary = json.loads((self.trace.directory/'summary.json').read_text())
        self.assertEqual(summary['error_message'], str(caught.exception))
        events = json.loads((self.trace.directory/'events.json').read_text())
        diagnostic = next(e for e in events if e['event']=='model_request_error')
        self.assertEqual(diagnostic['finish_reason'], 'length')
        self.assertNotIn('private reasoning', json.dumps(diagnostic))

    async def test_failed_check_output_is_kept_in_log(self):
        (self.root/'value.txt').write_text('wrong')
        self.app.request = AsyncMock(side_effect=[
            {'role':'assistant','content':None,'tool_calls':[{'id':'check','type':'function',
             'function':{'name':'run_check','arguments':'{"name":"unit"}'}}]},
            {'role':'assistant','content':'Check failed.'}])
        await self.app.run('director', [], {'calls':4}, {}, self.trace, self.task)
        event = next(e for e in self.trace.events if e['event']=='tool_finished')
        self.assertFalse(event['result']['passed'])
        self.assertIn('AssertionError', event['result']['output'])
