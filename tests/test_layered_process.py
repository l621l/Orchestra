"""Offline progress, role policies, diagnostics and bounded worker recovery."""
import json
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from no_progress import EditProgress
from verification import Verification
from orchestra import validate_settings
from runtime_settings import load_runtime_settings
from tests import test_base_tools
from tests import test_worker_workspaces as fixtures
from tool_errors import ToolError


class ProgressPolicy(unittest.TestCase):
    def test_one_passing_check_does_not_complete_the_verification_stage(self):
        verification = Verification(['unit', 'integration'])
        verification.edited()
        verification.checked('unit', True)
        self.assertEqual(verification.summary()['status'], 'not_run')
        verification.checked('integration', True)
        self.assertEqual(verification.summary()['status'], 'passed')
    def test_recovery_budget_can_be_smaller_than_policy_and_verification_is_a_stage(self):
        now = [0.0]
        policy = dict(enabled=True, calls_without_edit=3, seconds_without_edit=120,
                      recovery_calls=2, recovery_seconds=90)
        guard = EditProgress(policy, clock=lambda: now[0])
        guard.calls = 3
        guard.recover(calls=1, seconds=20)
        self.assertEqual(guard.snapshot()['recovery_calls_available'], 1)
        guard.calls += 1
        self.assertEqual(guard.state(), 'exhausted')
        guard.edited()
        guard.enter_stage('verification')
        now[0] = 1000
        self.assertEqual(guard.state(), 'verification')
        guard.enter_stage('repair')
        self.assertEqual(guard.state(), 'working')
        self.assertEqual(guard.snapshot()['seconds_without_edit'], 1000)
    def test_calls_time_recovery_and_real_edit_reset(self):
        now = [0.0]
        policy = dict(enabled=True, calls_without_edit=3, seconds_without_edit=120,
                      recovery_calls=2, recovery_seconds=90)
        p = EditProgress(policy, clock=lambda: now[0])
        p.calls = 3
        self.assertEqual(p.state(), 'warn')
        p.recover(); p.calls = 4
        self.assertEqual(p.state(), 'recovery')
        p.calls = 5
        self.assertEqual(p.state(), 'exhausted')
        p.edited()
        self.assertEqual(p.state(), 'working')
        now[0] = 120
        self.assertEqual(p.state(), 'warn')
        p.recover(); now[0] = 210
        self.assertEqual(p.state(), 'exhausted')

    def test_profile_budget_and_independent_thinking_switch(self):
        settings = load_runtime_settings('settings.json', 'comparison')
        self.assertEqual(settings['execution']['max_comparison_rounds'], 10)
        self.assertEqual(settings['execution']['max_api_calls_per_turn'], 60)
        self.assertEqual(settings['execution']['turn_timeout_seconds'], 2700)
        self.assertEqual(settings['roles']['worker']['thinking'], 'enabled')
        self.assertTrue(settings['roles']['worker']['no_progress']['enabled'])
        settings['roles']['worker']['thinking'] = 'disabled'
        validate_settings(settings)
        for value in [0, True, float('nan')]:
            settings['roles']['worker']['no_progress']['seconds_without_edit'] = value
            with self.assertRaises(ValueError):
                validate_settings(settings)


class WorkerProgress(unittest.IsolatedAsyncioTestCase):
    setUp = test_base_tools.RuntimeContracts.setUp
    call = test_base_tools.RuntimeContracts.call
    response = test_base_tools.RuntimeContracts.response

    def configure(self):
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        self.app.settings['roles']['worker']['no_progress'].update(
            calls_without_edit=2, recovery_calls=2, seconds_without_edit=10000,
            recovery_seconds=10000)
        self.app.isolated_worker = True
        (self.root / 'spec.md').write_text('One value.', encoding='utf-8')

    async def run_worker(self, responses, calls=8):
        self.app.request = AsyncMock(side_effect=responses)
        messages = []
        result = await self.app.run('worker', messages, {'calls': calls}, {}, self.trace,
            self.task, max_calls=calls, mutation_requested=True)
        return result, messages

    async def test_reading_does_not_reset_gap_and_bounded_recovery_returns_no_progress(self):
        self.configure()
        result, messages = await self.run_worker([
            *[self.response('read_file', {'path': 'spec.md'}) for _ in range(4)],
            {'role': 'assistant', 'content': 'Partial: no edits; split this task.'}])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['stop_reason'], 'no_progress')
        self.assertEqual(self.app.request.call_count, 5)
        self.assertTrue(self.app.request.call_args.args[8])
        self.assertTrue(any('NO-PROGRESS RECOVERY' in str(m.get('content')) for m in messages))
        self.assertEqual([c.kwargs['thinking_enabled'] for c in self.app.request.call_args_list],
                         [True, True, False, False, False])

    async def test_six_calls_leave_only_one_recovery_call_after_three_attempts(self):
        self.configure()
        self.app.settings['roles']['worker']['no_progress']['calls_without_edit'] = 3
        result, _ = await self.run_worker([
            *[self.response('read_file', {'path': 'spec.md'}) for _ in range(4)],
            {'role': 'assistant', 'content': 'Partial: no progress.'}], calls=6)
        event = next(e for e in self.trace.events if e['event'] == 'no_progress_recovery_started')
        self.assertEqual(event['recovery_calls_available'], 1)
        self.assertEqual(self.app.request.call_count, 5)
        self.assertEqual(result['stop_reason'], 'no_progress')

    async def test_last_recovery_edit_is_checked_before_report_without_spending_reserve(self):
        self.configure()
        self.app.settings['roles']['worker']['no_progress']['calls_without_edit'] = 3
        async def reply(*args, **kwargs):
            index = self.app.request.call_count
            if index <= 3:
                return self.response('read_file', {'path': 'spec.md'})
            if index == 4:
                return self.response('create_file', {'path': 'new.py', 'content': 'value = 2\n'})
            self.assertTrue(args[8])
            self.assertIn('verification_report_reserve', args[3])
            self.assertTrue(any(e['event'] == 'automatic_verification_finished' for e in self.trace.events))
            return {'role': 'assistant', 'content': 'Implemented and verified.'}
        result, _ = await self.run_worker(reply, calls=6)
        self.assertEqual(self.app.request.call_count, 5)
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual(result['status'], 'completed')

    async def test_elapsed_threshold_does_not_cancel_completed_edit(self):
        self.configure()
        self.app.settings['roles']['worker']['no_progress'].update(
            calls_without_edit=10, seconds_without_edit=.01, recovery_seconds=.01)
        async def reply(*args, **kwargs):
            if self.app.request.call_count == 1:
                await asyncio.sleep(.05)
                return self.response('create_file', {'path': 'new.py', 'content': 'value = 2\n'})
            if self.app.request.call_count == 2:
                return self.response('run_check', {'name': 'unit'})
            return {'role': 'assistant', 'content': 'Implemented and checked.'}
        result, _ = await self.run_worker(reply)
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 2\n')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertFalse(any(e['event'] in {'no_progress_request_interrupted',
            'no_progress_stopped'} for e in self.trace.events))

    async def test_runtime_deadline_still_cancels_adapter_request(self):
        self.configure()
        cancelled = []
        async def stalled(*args, **kwargs):
            if args[8]:
                return {'role': 'assistant', 'content': 'Partial: deadline reached.'}
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        self.app.request = AsyncMock(side_effect=stalled)
        result = await self.app.run('worker', [], {'calls': 8}, {}, self.trace,
            self.task, max_calls=8, mutation_requested=True,
            turn_deadline=time.monotonic() + 60.05)
        self.assertEqual(cancelled, [True])
        event = next(e for e in self.trace.events if e['event'] == 'request_interrupted')
        self.assertEqual(event['reason'], 'runtime_deadline')
        self.assertEqual(result['status'], 'partial')

    async def test_repeating_same_failed_check_does_not_restart_repair_clock(self):
        self.configure()
        self.app.settings['roles']['worker']['no_progress']['recovery_calls'] = 1
        result, _ = await self.run_worker([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 1\n'}),
            *[self.response('run_check', {'name': 'unit'}) for _ in range(4)],
            {'role': 'assistant', 'content': 'Partial: repair made no progress.'}], calls=10)
        self.assertEqual(result['stop_reason'], 'no_progress')
        self.assertEqual(self.app.request.call_count, 6)
        self.assertEqual(result['verification']['status'], 'failed')

    async def test_targeted_read_then_edit_resets_gap_and_check_completes(self):
        self.configure()
        result, _ = await self.run_worker([
            self.response('read_file', {'path': 'spec.md'}),
            self.response('read_file', {'path': 'spec.md'}),
            self.response('read_file', {'path': 'spec.md', 'start_line': 1, 'end_line': 1}),
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2\n'}),
            self.response('run_check', {'name': 'unit'}),
            {'role': 'assistant', 'content': 'Implemented and checked.'}])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        events = [e for e in self.trace.events if e['event'] == 'no_progress_stopped']
        self.assertEqual(events, [])

    async def test_elapsed_threshold_warns_between_completed_read_responses(self):
        self.configure()
        policy = self.app.settings['roles']['worker']['no_progress']
        policy.update(seconds_without_edit=120, recovery_seconds=90)
        now = [0.0]
        guard = EditProgress(policy, clock=lambda: now[0])
        async def reply(*args, **kwargs):
            index = self.app.request.call_count
            self.assertIsNone(kwargs['request_deadline'])
            if index <= 2:
                self.assertEqual(kwargs['thinking_enabled'], index == 1)
                now[0] += 121 if index == 1 else 91
                return self.response('read_file', {'path': 'spec.md'})
            self.assertTrue(args[8])
            return {'role': 'assistant', 'content': 'Partial: no progress.'}
        with patch('orchestra.EditProgress', return_value=guard):
            result, _ = await self.run_worker(reply)
        self.assertEqual(self.app.request.call_count, 3)
        self.assertEqual(result['stop_reason'], 'no_progress')
        self.assertEqual(sum(e['event'] == 'tool_finished' and e['tool'] == 'read_file'
                             for e in self.trace.events), 2)
        self.assertFalse(any(e['event'] == 'request_interrupted' for e in self.trace.events))

    async def test_recovery_response_edit_is_executed_after_soft_deadline(self):
        self.configure()
        policy = self.app.settings['roles']['worker']['no_progress']
        now = [0.0]
        guard = EditProgress(policy, clock=lambda: now[0])
        async def reply(*args, **kwargs):
            index = self.app.request.call_count
            if index <= 2:
                return self.response('read_file', {'path': 'spec.md'})
            if index == 3:
                self.assertFalse(kwargs['thinking_enabled'])
                now[0] += policy['recovery_seconds'] + 1
                return self.response('create_file', {'path': 'new.py', 'content': 'value = 2\n'})
            if index == 4:
                return self.response('run_check', {'name': 'unit'})
            return {'role': 'assistant', 'content': 'Implemented and checked.'}
        with patch('orchestra.EditProgress', return_value=guard):
            result, _ = await self.run_worker(reply)
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 2\n')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertFalse(any(e['event'] == 'no_progress_stopped' for e in self.trace.events))

    async def test_recovery_thinking_can_be_enabled_in_role_configuration(self):
        self.configure()
        self.app.settings['roles']['worker']['recovery_thinking'] = 'enabled'
        await self.run_worker([
            *[self.response('read_file', {'path': 'spec.md'}) for _ in range(4)],
            {'role': 'assistant', 'content': 'Partial: split the task.'}])
        self.assertTrue(all(c.kwargs['thinking_enabled'] for c in self.app.request.call_args_list[:-1]))
        self.assertFalse(self.app.request.call_args.kwargs['thinking_enabled'])

    async def test_real_stream_timeout_recovery_edit_check_report_and_baseline_reset(self):
        self.configure()
        self.app.settings['roles']['worker']['no_progress'].update(
            calls_without_edit=10, seconds_without_edit=.03, recovery_seconds=10)
        self.app.settings['api']['request_timeout_seconds'] = .05
        self.app.role_settings.return_value = {'provider': 'mimo', 'model': 'offline-model'}
        self.app.pricing = {}
        cancelled = []

        class Stream:
            def __init__(self, message=None):
                self.message = message

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def __aiter__(self):
                async def chunks():
                    if self.message is None:
                        # A timed-out stream may already contain incomplete code.
                        # It must never execute before the complete recovery reply.
                        partial = SimpleNamespace(index=0, id='unfinished', function=SimpleNamespace(
                            name='create_file', arguments='{"path":"new.py","content":"unfinished'))
                        yield SimpleNamespace(id='offline', model='offline-model', usage=None,
                            choices=[SimpleNamespace(finish_reason=None, delta=SimpleNamespace(
                                content=None, reasoning_content=None, tool_calls=[partial]))])
                        try:
                            await asyncio.sleep(5)
                        except asyncio.CancelledError:
                            cancelled.append(True)
                            raise
                    calls = [SimpleNamespace(index=i, id=call['id'],
                             function=SimpleNamespace(**call['function']))
                             for i, call in enumerate(self.message.get('tool_calls', []))]
                    yield SimpleNamespace(id='offline', model='offline-model', usage=None,
                        choices=[SimpleNamespace(finish_reason='tool_calls' if calls else 'stop',
                            delta=SimpleNamespace(content=self.message.get('content'),
                                reasoning_content=None, tool_calls=calls))])
                return chunks()

        create = AsyncMock(side_effect=[Stream(),
            Stream(self.response('create_file', {'path': 'new.py', 'content': 'value = 2\n'})),
            Stream(self.response('run_check', {'name': 'unit'})),
            Stream({'role': 'assistant', 'content': 'Implemented and checked.'})])
        self.app.clients = {'mimo': SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))}
        result = await self.app.run('worker', [], {'calls': 5}, {}, self.trace,
            self.task, max_calls=5, mutation_requested=True)
        self.assertEqual(cancelled, [True])
        self.assertEqual([c.kwargs['extra_body']['thinking']['type'] for c in create.call_args_list],
                         ['enabled', 'disabled', 'enabled', 'disabled'])
        self.assertIn('tools', create.call_args_list[1].kwargs)
        self.assertNotIn('tools', create.call_args_list[-1].kwargs)
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 2\n')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual(result['status'], 'completed')
        recovery = next(e for e in self.trace.events if e['event'] == 'no_progress_recovery_started')
        self.assertEqual(recovery['state'], 'recovery')
        requests = [e for e in self.trace.events if e['event'] == 'model_request_finished']
        self.assertEqual(requests[0]['error_type'], 'TimeoutError')
        interrupted = next(e for e in self.trace.events if e['event'] == 'request_interrupted')
        self.assertEqual(interrupted['reason'], 'request_timeout')
        self.assertEqual(requests[0]['response']['tool_names'], ['create_file'])
        self.assertEqual(requests[1]['request']['thinking'], 'disabled')
        edits = [e for e in self.trace.events
                 if e['event'] == 'tool_finished' and e['tool'] == 'create_file']
        self.assertEqual(len(edits), 1)

    async def test_disabled_worker_thinking_is_independent_of_guard(self):
        self.configure()
        self.app.settings['roles']['worker']['thinking'] = 'disabled'
        result, _ = await self.run_worker([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2\n'}),
            self.response('run_check', {'name': 'unit'}),
            {'role': 'assistant', 'content': 'Done.'}])
        self.assertFalse(self.app.request.call_args_list[0].kwargs['thinking_enabled'])
        self.assertEqual(result['status'], 'completed')

    async def test_research_worker_is_not_required_to_edit(self):
        self.configure()
        self.app.isolated_worker = False
        self.app.request = AsyncMock(return_value={'role': 'assistant', 'content': 'Research finding.'})
        result = await self.app.run('worker', [], {'calls': 6}, {}, self.trace,
            self.task, max_calls=6, final_answer_target_tokens=500)
        self.assertEqual(result['status'], 'completed')
        event = next(e for e in self.trace.events if e['event'] == 'progress_policy')
        self.assertFalse(event['enabled'])

    def test_unknown_usage_and_first_edit_diagnostics(self):
        self.trace.record_request(self.task, 'missing', None, 'p', 'm', 'm', 1, None,
                                  None, 'TimeoutError', {}, {})
        summary = json.loads((self.trace.directory / 'summary.json').read_text())
        self.assertEqual(summary['usage']['usage_status'], 'unknown')
        self.assertIsNone(summary['usage']['total_tokens'])
        usage = dict(prompt_tokens=100, completion_tokens=30, total_tokens=130,
                     prompt_tokens_details={'cached_tokens': 90},
                     completion_tokens_details={'reasoning_tokens': 20})
        self.trace.record_request(self.task, 'known', None, 'p', 'm', 'm', 1, 'length', usage, None, {}, {})
        self.trace.record_tool(self.task, 'worker-1', 'edit', 'c', 'create_file',
            {'path': 'new.py'}, .1, True, {'files': ['new.py'], 'files_changed': 1})
        self.trace.record_tool(self.task, 'worker-1', 'check', 'c2', 'run_check',
            {'name': 'unit'}, .1, True, {'passed': True})
        self.trace.flush_summary()
        summary = json.loads((self.trace.directory / 'summary.json').read_text())
        metrics = summary['root_task']['metrics']
        self.assertEqual(metrics['calls_to_first_edit'], 2)
        self.assertIsNotNone(metrics['first_edit_seconds'])
        self.assertEqual((metrics['timeouts'], metrics['length_stops']), (1, 1))
        self.assertTrue(metrics['completed_checks'][0]['passed'])
        self.assertEqual(summary['usage']['cached_tokens'], 90)
        self.assertEqual(summary['usage']['reasoning_tokens'], 20)
        self.assertEqual(summary['usage']['usage_status'], 'partial')
        self.assertEqual(summary['specification_status'], 'not_assessed_by_runtime')


class RetryScope(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ComparisonRuntime.setUp
    prepare = fixtures.ComparisonRuntime.prepare

    async def test_worker_stop_reason_survives_and_identical_retry_is_rejected(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        self.app.run = AsyncMock(return_value={'answer': 'Split the task.', 'status': 'partial',
            'result_source': 'final', 'stop_reason': 'no_progress'})
        first = await scheduler.start_comparison('Task CRUD and dependencies.', 3, 500, self.task, acceptance_checks=['Value is verified'])
        result = await scheduler.wait(until_event=True)
        self.assertTrue(all(t['stop_reason'] == 'no_progress' for t in result['tasks']))
        self.assertTrue(all('retry_guidance' in t for t in result['tasks']))
        scheduler.reject_comparison(first['round_id'], 'Reduce scope.')
        with self.assertRaises(ToolError) as caught:
            await scheduler.start_comparison('Task CRUD and dependencies.', 3, 500, self.task, acceptance_checks=['Value is verified'])
        self.assertEqual(caught.exception.code, 'task_scope_not_reduced')
        second = await scheduler.start_comparison('Only add and show one task.', 3, 500, self.task, acceptance_checks=['Value is verified'])
        self.assertNotEqual(first['round_id'], second['round_id'])
        await scheduler.wait(until_event=True)
