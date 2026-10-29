"""Readable, bounded console actions with real Rich rendering."""
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest

from rich.console import Console
from orchestra import AgentDashboard, Orchestra, validate_settings
from runtime_settings import load_runtime_settings
from tests import test_worker_workspaces as fixtures


class Dashboard(unittest.TestCase):
    def test_live_usage_snapshots_do_not_double_count_and_mark_missing_reports(self):
        dashboard = AgentDashboard()
        for label in ('director', 'worker-1'):
            dashboard.update(label)
        entry = {'api_calls': 1, 'reported_calls': 0, 'total_tokens': 0}
        dashboard.set_usage('director', entry, pending=True)
        dashboard.set_usage('worker-1', entry, pending=True)
        text = self.render_text(dashboard)
        self.assertIn('Calls', text)
        self.assertIn('Tokens', text)
        self.assertNotIn('0*?', text)  # Two in-flight calls are not missing reports.
        entry.update(reported_calls=1, total_tokens=1234)
        dashboard.set_usage('director', entry)
        dashboard.set_usage('director', entry)  # Refresh is a snapshot, not addition.
        self.assertEqual(dashboard.usage_cells(dashboard.usage['director']), ('1', '1.2k'))
        dashboard.set_usage('worker-1', {'api_calls': 1, 'reported_calls': 0, 'total_tokens': 0})
        self.assertEqual(dashboard.usage_cells(dashboard.usage['worker-1']), ('1', 'unknown'))
        entry['total_tokens'] = 9999
        self.assertEqual(dashboard.usage['director']['total_tokens'], 1234)
        self.assertIn('1.2k?', self.render_text(dashboard))

    def render_text(self, dashboard, width=140):
        output = io.StringIO()
        dashboard.console = Console(file=output, width=width, height=40, color_system=None)
        dashboard.console.print(dashboard.render())
        return output.getvalue()

    def test_actions_keep_round_identity_and_successful_commit(self):
        dashboard = AgentDashboard()
        dashboard.register_round('round_1', [{'agent': 'worker-1', 'task_id': 'task_a', 'alternative': 'A'}])
        dashboard.update('worker-1', 1, 'reasoning')
        dashboard.tool_finished('worker-1', 'create_file', {'path': 'src/main.py', 'content': 'SECRET CODE'},
            {'ok': True, 'result': {'files_changed': 1}}, .5)
        dashboard.tool_finished('director', 'promote_candidate', {'task_id': 'task_a'},
            {'ok': True, 'result': {'promoted': True, 'commit': 'abcdef012345'}}, 2)
        text = self.render_text(dashboard)
        for expected in ('R1/A', 'worker-1', 'src/main.py', 'accepted -> abcdef01'):
            self.assertIn(expected, text)
        self.assertNotIn('SECRET CODE', text)

    def test_failures_and_markup_are_literal_not_false_success(self):
        dashboard = AgentDashboard()
        dashboard.update('worker-1', 1, 'reading [red]file')
        dashboard.tool_finished('worker-1', 'read_file', {'path': '[red].py', 'start_line': 2, 'end_line': 8},
            {'ok': False, 'error': 'path_forbidden'})
        dashboard.tool_finished('worker-1', 'run_check', {'name': 'unit'},
            {'ok': True, 'result': {'passed': False}})
        text = self.render_text(dashboard)
        self.assertIn('[red].py:2-8', text)
        self.assertIn('ERROR', text)
        self.assertIn('FAILED', text)
        self.assertIn('path_forbidden', text)

    def test_buffer_is_bounded_and_small_terminal_does_not_wrap_actions(self):
        dashboard = AgentDashboard(recent_actions=2)
        for i in range(5):
            dashboard.tool_finished('worker-1', 'read_file', {'path': f'file_{i}.py'},
                                    {'ok': True, 'result': {}})
        self.assertEqual(len(dashboard.actions), 2)
        text = self.render_text(dashboard, 80)
        self.assertNotIn('file_0.py', text)
        self.assertIn('file_4.py', text)
        self.assertTrue(all(len(line) <= 80 for line in text.splitlines()))

    def test_control_codes_are_removed_and_activity_can_be_disabled(self):
        dashboard = AgentDashboard(recent_actions=0)
        dashboard.tool_finished('worker-1', 'read_file', {'path': 'a.py'}, {'ok': True, 'result': {}})
        self.assertFalse(dashboard.actions)
        self.assertNotIn('\x1b', dashboard.short('file\x1b[2J\nname'))
        for value in (-1, True, 31):
            settings = load_runtime_settings('settings.json')
            settings['ui']['recent_actions'] = value
            with self.assertRaises(ValueError):
                validate_settings(settings)

    def test_rejected_malformed_arguments_cannot_break_the_dashboard(self):
        dashboard = AgentDashboard()
        for name, args in [('read_file', {'path': None, 'start_line': []}),
                           ('review_task_changes', {'task_id': [], 'path': {}})]:
            dashboard.tool_finished('director', name, args,
                                    {'ok': False, 'error': 'invalid_arguments'})
        self.assertEqual(len(dashboard.actions), 2)
        self.assertIn('invalid_arguments', self.render_text(dashboard))


class UsageSummary(unittest.TestCase):
    def summary(self, reported=1, calls=1, priced=True):
        app = Orchestra.__new__(Orchestra)
        app.pricing = {'providers': {'test': {'model': dict(
            input_cache_miss=1, input_cache_hit=1, output=1)}}} if priced else {}
        output = io.StringIO()
        app.dashboard = SimpleNamespace(console=Console(file=output, width=250, color_system=None))
        entry = dict(provider='test', model='model', api_calls=calls, reported_calls=reported,
                     prompt_tokens=800 * reported, cached_tokens=0,
                     completion_tokens=200 * reported, reasoning_tokens=0,
                     total_tokens=1000 * reported)
        with redirect_stdout(output):
            app.print_usage_summary({'director': entry})
        return output.getvalue()

    def test_complete_cost_and_final_total_are_visible_without_color(self):
        text = self.summary()
        self.assertIn('FINAL TOTAL', text)
        total = next(line for line in text.splitlines() if line.startswith('TOTAL:'))
        self.assertIn('estimated=$0.00100', total)
        self.assertNotIn('partial', total)

    def test_missing_usage_keeps_known_subtotal_without_claiming_full_cost(self):
        text = self.summary(calls=2)
        total = next(line for line in text.splitlines() if line.startswith('TOTAL:'))
        self.assertIn('estimated_known=$0.00100', total)
        self.assertIn('full cost unknown', total)
        self.assertIn('usage missing for 1 call(s)', total)

    def test_no_usage_or_no_pricing_does_not_display_a_false_zero_cost(self):
        for text in (self.summary(reported=0), self.summary(priced=False)):
            total = next(line for line in text.splitlines() if line.startswith('TOTAL:'))
            self.assertIn('estimated=unknown', total)
            self.assertNotIn('$0.00000', total)


class DashboardIntegration(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ComparisonRuntime.setUp
    call = fixtures.ComparisonRuntime.call
    response = fixtures.ComparisonRuntime.response

    def prepare(self):
        scheduler = fixtures.ComparisonRuntime.prepare(self)
        self.app.dashboard = AgentDashboard(recent_actions=30)
        return scheduler

    async def test_real_dashboard_records_both_rounds_checks_and_promotions(self):
        await fixtures.ComparisonRuntime.test_two_rounds_choose_whole_candidates_and_rebase_next_pair(self)
        dashboard = self.app.dashboard
        self.assertEqual(set(dashboard.agent_rounds.values()), {'R1/A', 'R1/B', 'R2/A', 'R2/B'})
        details = [row[3] for row in dashboard.actions]
        self.assertEqual(sum('accepted ->' in row for row in details), 2)
        self.assertTrue(any('run_check unit | passed' in row for row in details))
