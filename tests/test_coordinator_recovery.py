"""Replay coordinator length recovery, waits, reviews and mistaken task IDs."""
import json
import unittest
from unittest.mock import AsyncMock

from runtime_settings import load_runtime_settings
from orchestra import TaskScheduler, validate_settings
from tests import test_worker_workspaces as fixtures


class CoordinatorRecovery(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ComparisonRuntime.setUp
    prepare = fixtures.ComparisonRuntime.prepare
    call = fixtures.ComparisonRuntime.call
    response = fixtures.ComparisonRuntime.response

    async def replay(self, event_wait):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        scheduler.shared_budget['calls'] = 30
        wait = scheduler.wait
        async def chosen_wait(**kwargs):
            kwargs['until_event'] = event_wait
            result = await wait(**kwargs)
            self.assertTrue(result['all_finished'])
            self.assertEqual({r['task_id'] for r in result['tasks']}, set(scheduler.tasks))
            return result
        scheduler.wait = chosen_wait
        counts = {}

        async def respond(*args, **kwargs):
            role, label = args[0], args[7]
            index = counts.get(label, 0)
            counts[label] = index + 1
            if role == 'worker':
                if index == 0:
                    return self.response('create_file', {'path': 'new.py', 'content': 'value = 2\n'})
                return {'role': 'assistant', 'content': 'Implemented and checked.'}
            if index == 0:
                return {'role': 'assistant', 'content': None, '_truncated': True, '_partial': False}
            if index == 1:
                self.assertFalse(kwargs['thinking_enabled'])
                return self.response('start_comparison_round', {'acceptance_checks': ['Value is verified'],
                    'task': 'Implement one value.', 'max_calls_per_worker': 3,
                    'report_target_tokens': 500})
            if index == 2:
                self.assertTrue(kwargs['thinking_enabled'])
                return self.response('wait_tasks', {})
            current = scheduler.rounds[scheduler.active_round_id] if scheduler.active_round_id else None
            if index == 3:
                for key in scheduler.tasks:
                    self.assertIn(key, args[3])  # IDs remain visible after collection.
                return self.response('review_task_changes', {'task_id': 'task_invented'})
            if index == 4:
                error = json.loads(args[1][-1]['content'])
                self.assertEqual(error['error'], 'unknown_task_id')
                self.assertEqual({t['task_id'] for t in error['details']['current_state']['tasks']}, set(scheduler.tasks))
                return {'role': 'assistant', 'content': None, 'tool_calls': [
                    self.call('review_task_changes', {'task_id': key}) for key in current['task_ids']]}
            if index == 5:
                key = current['task_ids'][0]
                return self.response('promote_candidate', {'decision': 'accept', 'acceptance': [{'id': 'C1', 'check': 'unit', 'evidence': 'Configured unit check imports new.value and asserts its value.'}], 'round_id': current['round_id'],
                    'task_id': key, 'fingerprint': scheduler.workspaces.review(key)['fingerprint'],
                    'message': 'feat: accept recovered milestone', 'rationale': 'Checks pass.'})
            return {'role': 'assistant', 'content': 'Milestone accepted.'}

        self.app.request = AsyncMock(side_effect=respond)
        result = await self.app.run('director', [], scheduler.shared_budget, {}, self.trace,
            self.task, scheduler=scheduler, max_delegations=2, mutation_requested=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 2\n')
        self.assertEqual(counts['director'], 7)

    async def test_recovery_wait_review_and_promotion_with_event_wait(self):
        await self.replay(True)

    async def test_same_recovery_and_task_ids_with_legacy_wait(self):
        await self.replay(False)

    async def test_repeated_unknown_ids_stop_before_spending_remaining_turn(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'project')
        self.app.settings['execution']['task_reference_retry_limit'] = 2
        director_calls = 0
        async def respond(*args, **kwargs):
            nonlocal director_calls
            if args[0] == 'worker':
                return {'role': 'assistant', 'content': 'Useful saved finding.'}
            director_calls += 1
            if director_calls == 1:
                return self.response('wait_tasks', {})
            if director_calls in (2, 3):
                return self.response('wait_tasks', {'task_ids': ['task_invented']})
            self.assertTrue(args[8])
            self.assertIn('finalization reason=task_reference_errors', args[3])
            return {'role': 'assistant', 'content': 'Partial: invalid task references; finding retained.'}
        self.app.request = AsyncMock(side_effect=respond)
        scheduler.submit('worker-1', 'Inspect.', self.task, 2, 500)
        result = await self.app.run('director', [], scheduler.shared_budget, {}, self.trace,
            self.task, scheduler=scheduler)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(director_calls, 4)
        self.assertGreater(scheduler.shared_budget['calls'], 0)
        self.assertEqual(next(iter(scheduler.tasks.values()))['answer'], 'Useful saved finding.')

    def test_retry_limit_validation(self):
        for value in (0, True, '3'):
            settings = load_runtime_settings('settings.json')
            settings['execution']['task_reference_retry_limit'] = value
            with self.assertRaises(ValueError):
                validate_settings(settings)
