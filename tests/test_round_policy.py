"""Stage retries retain their contract; workers leave time for review."""
import time
import unittest
from unittest.mock import AsyncMock

from orchestra import validate_settings
from runtime_settings import load_runtime_settings
from tool_errors import ToolError
from tests import test_worker_workspaces as fixtures


class RoundPolicy(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.EditingWorkerRuntime.setUp
    prepare = fixtures.EditingWorkerRuntime.prepare

    def configure(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        self.app.run = AsyncMock(return_value={'answer': 'No implementation.', 'status': 'partial',
                                               'result_source': 'final'})
        return scheduler

    async def rejected_stage(self):
        scheduler = self.configure()
        criteria = ['Behavior verified', 'README reviewed', 'REQUIREMENTS reviewed']
        started = await scheduler.start_comparison('Implement S01 from spec.md.', 2, 500,
            self.task, acceptance_checks=criteria)
        await scheduler.wait(until_event=True)
        scheduler.reject_comparison(started['round_id'], 'Missing documentation.')
        return scheduler, criteria

    async def test_rejected_stage_cannot_drop_rewrite_reorder_or_skip_contract(self):
        scheduler, criteria = await self.rejected_stage()
        count = len(scheduler.tasks)
        budget = scheduler.shared_budget['calls']
        head = self.app.repository._run(['rev-parse', 'HEAD'])['stdout']
        for task, proposed, stage, expected in [
            ('Repair S01.', criteria[:-1], None, 'comparison_contract_changed'),
            ('Repair S01.', [criteria[0], 'Docs later', criteria[2]], None, 'comparison_contract_changed'),
            ('Repair S01.', list(reversed(criteria)), None, 'comparison_contract_changed'),
            ('Repair the candidate.', criteria[:-1], None, 'comparison_contract_changed'),
            ('Implement S02.', criteria, None, 'comparison_stage_incomplete'),
            ('A new stage.', criteria, 'S02', 'comparison_stage_incomplete'),
        ]:
            with self.subTest(task=task, proposed=proposed), self.assertRaises(ToolError) as error:
                await scheduler.start_comparison(task, 2, 500, self.task,
                    acceptance_checks=proposed, stage_id=stage)
            self.assertEqual(error.exception.code, expected)
        self.assertEqual(len(scheduler.tasks), count)
        self.assertEqual(scheduler.shared_budget['calls'], budget)
        self.assertEqual(self.app.repository._run(['rev-parse', 'HEAD'])['stdout'], head)

    async def test_corrected_assignment_reuses_complete_contract_and_stage(self):
        scheduler, criteria = await self.rejected_stage()
        started = await scheduler.start_comparison('Repair implementation from spec.md.', 2, 500,
            self.task, acceptance_checks=criteria)
        self.assertEqual(started['stage_id'], 'S01')
        self.assertEqual([r['description'] for r in started['acceptance_checks']], criteria)
        await scheduler.wait(until_event=True)

    async def test_workers_receive_earlier_deadline_for_review_and_promotion(self):
        scheduler = self.configure()
        now = time.monotonic()
        scheduler.turn_deadline = now + 1200
        scheduler.finalization_deadline = now + 1080
        expected = scheduler.finalization_deadline - 300
        started = await scheduler.start_comparison('Implement S01.', 2, 500, self.task,
            acceptance_checks=['Behavior verified'])
        await scheduler.wait(until_event=True)
        self.assertEqual(len(self.app.run.call_args_list), 2)
        for call in self.app.run.call_args_list:
            self.assertEqual(call.kwargs['turn_deadline'], expected)
        event = next(e for e in self.trace.events if e['event'] == 'comparison_round_started')
        self.assertEqual(event['review_reserve_seconds'], 300)
        self.assertEqual(started['stage_id'], 'S01')

    async def test_late_round_is_refused_before_allocating_calls_or_workspaces(self):
        scheduler = self.configure()
        scheduler.turn_deadline = time.monotonic() + 500
        scheduler.finalization_deadline = time.monotonic() + 400
        budget = scheduler.shared_budget['calls']
        with self.assertRaises(ToolError) as error:
            await scheduler.start_comparison('Implement S01.', 2, 500, self.task,
                acceptance_checks=['Behavior verified'])
        self.assertEqual(error.exception.code, 'comparison_time_reserve')
        self.assertEqual(scheduler.tasks, {})
        self.assertEqual(scheduler.rounds, {})
        self.assertEqual(scheduler.shared_budget['calls'], budget)

    def test_review_reserve_validation_and_forty_five_minute_profiles(self):
        for profile in ('comparison', 'project'):
            settings = load_runtime_settings('settings.json', profile)
            self.assertEqual(settings['execution']['turn_timeout_seconds'], 2700)
            validate_settings(settings)
        settings = load_runtime_settings('settings.json', 'comparison')
        self.assertEqual(settings['execution']['comparison_review_reserve_seconds'], 300)
        for value in (-1, True, float('nan'), 2580):
            settings['execution']['comparison_review_reserve_seconds'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings(settings)
    async def test_final_report_context_counts_only_promoted_rounds(self):
        runtime = self.app.run
        scheduler = self.configure()
        scheduler.rounds = {
            'accepted': {'status': 'promoted', 'stage_id': 'S01', 'round_id': 'accepted', 'commit': 'abc123'},
            'checkpoint': {'status': 'checkpointed', 'stage_id': 'S02', 'round_id': 'checkpoint'},
        }
        self.app.request = AsyncMock(return_value={'role': 'assistant', 'content': 'One stage accepted.'})
        await runtime('director', [], {'calls': 1}, {}, self.trace, self.task,
                      scheduler=scheduler, mutation_requested=False)
        resource = self.app.request.call_args.args[3]
        self.assertIn('"stage_id": "S01"', resource)
        self.assertIn('"commit": "abc123"', resource)
        self.assertNotIn('"stage_id": "S02"', resource)

class StagePromotion(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.EditingWorkerRuntime.setUp
    prepare = fixtures.EditingWorkerRuntime.prepare
    call = fixtures.EditingWorkerRuntime.call
    response = fixtures.EditingWorkerRuntime.response

    async def test_accepted_stage_allows_next_stage_with_its_own_contract(self):
        from tests import test_acceptance
        scheduler, started, key, fingerprint = await test_acceptance.AcceptanceRuntime.candidate(self, documentation=True)
        scheduler.rounds[started['round_id']]['stage_id'] = 'S01'
        result = scheduler.promote_comparison(started['round_id'], key, fingerprint,
            'Accept S01', 'Verified code and README', [
                {'id': 'C1', 'check': 'unit', 'evidence': 'Value assertion passed'},
                {'id': 'C2', 'files': ['README.md'], 'evidence': 'Launch instructions reviewed'}])
        self.assertTrue(result['promoted'])
        self.app.run = AsyncMock(return_value={'answer': 'No changes.', 'status': 'partial', 'result_source': 'final'})
        next_round = await scheduler.start_comparison('Implement S02.', 2, 500, self.task,
            acceptance_checks=['Storage tested'], stage_id='S02')
        await scheduler.wait(until_event=True)
        self.assertEqual(next_round['checkpoint']['head'], result['commit'])
        self.assertEqual(next_round['stage_id'], 'S02')

    async def test_checkpoint_retains_pending_stage_contract(self):
        from tests import test_acceptance
        scheduler, started, key, fingerprint = await test_acceptance.AcceptanceRuntime.candidate(self, documentation=True)
        scheduler.rounds[started['round_id']]['stage_id'] = 'S01'
        scheduler.promote_comparison(started['round_id'], key, fingerprint,
            'Partial S01', 'Documentation still unverified', [], decision='checkpoint')
        with self.assertRaises(ToolError) as error:
            await scheduler.start_comparison('Retry S01.', 2, 500, self.task,
                acceptance_checks=['value equals two'])
        self.assertEqual(error.exception.code, 'comparison_contract_changed')
