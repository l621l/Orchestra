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

    def test_reused_agent_restarts_timer(self):
        self.dashboard.update("tester")
        self.dashboard.finish("tester")
        self.assertEqual(self.dashboard.states["tester"]["status"],"done")
        self.dashboard.update("tester")
        self.assertEqual(self.dashboard.states["tester"]["status"],"running")
        self.assertIsNone(self.dashboard.states["tester"]["duration"])

    def test_actions_include_path_result_and_duration_without_markup(self):
        self.dashboard.tool_finished('worker', 'read_file', {'path':'[bold]sample.py'},
            {'ok':True, 'result':{'returned_start_line':1,'returned_end_line':12}}, .25)
        output = self.stream.getvalue()
        self.assertIn('[bold]sample.py', output)
        self.assertIn('lines 1-12', output)
        self.assertIn('0.25s', output)
        self.assertNotIn('\x1b', output)

    def test_failed_checks_and_tool_errors_show_actual_diagnostics(self):
        self.dashboard.tool_finished('worker','run_check',{'name':'unit'},
            {'ok':True,'result':{'passed':False,'returncode':1,'output':'Traceback\nAssertionError: wrong value'}},.5)
        self.dashboard.tool_finished('worker','replace_text',{'path':'a.py'},
            {'ok':False,'error':'text_not_found','message':'Read the current fragment.'},.1)
        output = self.stream.getvalue()
        self.assertIn('FAIL (exit=1)', output)
        self.assertIn('AssertionError: wrong value', output)
        self.assertIn('text_not_found', output)
        self.assertIn('Read the current fragment.', output)

    def test_output_tokens_are_a_compact_total_column(self):
        self.dashboard.update('worker')
        self.dashboard.update_output_budget(5000,None)
        self.dashboard.console.print(self.dashboard.render())
        output=self.stream.getvalue()
        self.assertIn('Output',output)
        self.assertIn('5k',output)
        self.assertNotIn('/20k',output)
        self.assertNotIn('remaining',output)
        self.assertIsNone(self.dashboard.render().caption)

    def test_provider_error_body_is_not_printed(self):
        error = RuntimeError('secret api key and provider body')
        error.status_code = 401
        description = error_description(error)
        self.assertIn('HTTP 401', description)
        self.assertIn('authentication', description)
        self.assertNotIn('secret', description)


class RequestDiagnostics(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SingleModel.setUp

    async def test_failed_check_output_is_kept_in_log(self):
        (self.root/'value.txt').write_text('wrong')
        self.app.request = AsyncMock(side_effect=[
            {'role':'assistant','content':None,'tool_calls':[{'id':'check','type':'function',
             'function':{'name':'run_check','arguments':'{"name":"unit"}'}}]},
            {'role':'assistant','content':'Check failed.'}])
        await self.app.run('worker', [], {'calls':4}, {}, self.trace, self.task)
        event = next(e for e in self.trace.events if e['event']=='tool_finished')
        self.assertFalse(event['result']['passed'])
        self.assertIn('AssertionError', event['result']['output'])
