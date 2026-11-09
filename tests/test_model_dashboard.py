import io
import unittest
from unittest.mock import patch
from rich.console import Console
from orchestra import AgentDashboard


class ModelDashboard(unittest.TestCase):
    def setUp(self):
        self.dashboard = AgentDashboard()
        self.dashboard.console = Console(file=io.StringIO(),width=180,color_system=None)
        self.dashboard.configure_models({'writer':'mimo-v2.6-pro','strong':'gpt-6.1-sol'})

    def render(self):
        self.dashboard.console.print(self.dashboard.render())
        return self.dashboard.console.file.getvalue()

    def test_tool_execution_is_attributed_to_actual_model(self):
        self.dashboard.activate_model('strong',6,6,next_model='mimo-v2.6-pro')
        self.dashboard.update('worker',activity='read_file game.py')
        self.dashboard.tool_finished('worker','read_file',{'path':'game.py'},
            {'ok':True,'result':{'returned_start_line':1,'returned_end_line':10}},.2)
        self.assertEqual(set(self.dashboard.states),{'strong'})
        text=self.render()
        self.assertIn('gpt-6.1-sol',text)
        self.assertIn('Step 6/6',text)
        self.assertIn('Next: mimo-v2.6-pro',text)
        self.assertNotIn('[worker]',text)
        self.assertNotIn('Stream',text)
        self.assertNotIn('â',text)

    def test_switch_marks_previous_model_waiting_and_keeps_usage(self):
        self.dashboard.activate_model('writer',5,6)
        self.dashboard.record_usage('writer',{'api_calls':5,'prompt_tokens':25000,'output_budget_tokens':400})
        self.dashboard.update('writer',cost=.02)
        self.dashboard.activate_model('strong',6,6)
        self.assertEqual(self.dashboard.states['writer']['status'],'waiting')
        self.assertEqual(self.dashboard.states['strong']['status'],'running')
        self.assertEqual(self.dashboard.states['writer']['calls'],5)
        self.assertEqual(self.dashboard.states['writer']['cost'],.02)
        self.dashboard.update('worker',activity='run_check python_tests')
        self.assertEqual(set(self.dashboard.states),{'writer','strong'})
        self.dashboard.finish('worker','partial','Work remains')
        self.assertEqual(self.dashboard.states['writer']['status'],'done')
        self.assertEqual(self.dashboard.states['strong']['status'],'partial')

    def test_two_models_and_budget_are_readable(self):
        with patch('orchestra.Live'):
            self.dashboard.start('turn','Build game')
        self.dashboard.activate_model('writer',1,6,next_model='mimo-v2.6-pro')
        self.dashboard.update_output_budget(416,20000)
        text=self.render()
        self.assertEqual(set(self.dashboard.states),{'writer','strong'})
        self.assertIn('mimo-v2.6-pro',text)
        self.assertIn('gpt-6.1-sol',text)
        self.assertIn('416/20k',text)
        self.assertIn('Output budget',text)
        self.assertNotIn('worker',text)

    def test_elapsed_time_accumulates_only_active_segments(self):
        with patch('orchestra.time.monotonic',return_value=10):
            self.dashboard.activate_model('writer',1,6)
        with patch('orchestra.time.monotonic',return_value=20):
            self.dashboard.activate_model('strong',6,6)
        with patch('orchestra.time.monotonic',return_value=50):
            self.dashboard.activate_model('writer',1,6)
        with patch('orchestra.time.monotonic',return_value=55):
            self.dashboard.finish('worker')
        self.assertEqual(self.dashboard.states['writer']['duration'],15)
        self.assertEqual(self.dashboard.states['strong']['duration'],30)
