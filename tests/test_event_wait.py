"""Event-driven director waits without paid polling or lost cancellation."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from execution_phase import ImplementationPhase
from orchestra import TaskScheduler
from runtime_settings import load_runtime_settings
from tests import test_base_tools


class EventWait(unittest.IsolatedAsyncioTestCase):
    setUp = test_base_tools.RuntimeContracts.setUp
    response = test_base_tools.RuntimeContracts.response
    call = test_base_tools.RuntimeContracts.call

    def scheduler(self):
        return TaskScheduler(self.app, {'calls': 20}, {}, self.trace)

    async def test_one_model_wait_spans_legacy_interval_and_ignores_project_research_clock(self):
        scheduler = self.scheduler()
        self.app.settings = load_runtime_settings('settings.json', 'project')
        counts = {'director': 0, 'worker': 0}

        async def respond(*args, **kwargs):
            role = args[0]
            counts[role] += 1
            if role == 'worker':
                await asyncio.sleep(1.05)
                return {'role': 'assistant', 'content': 'Confirmed finding.'}
            if counts[role] == 1:
                return self.response('wait_tasks', {'timeout_seconds': 1})
            self.assertTrue(all(r['collected'] for r in scheduler.tasks.values()))
            if counts[role] == 2:
                return self.response('report_blocker', {'reason': 'missing_information',
                    'evidence': 'Research received; implementation contract missing.',
                    'next_step': 'Clarify the contract.'})
            return {'role': 'assistant', 'content': 'Finding received; no implementation requested.'}

        self.app.request = AsyncMock(side_effect=respond)
        scheduler.submit('worker-1', 'Inspect.', self.task, 3, 500)
        def expired_phase(*args, **kwargs):
            phase = ImplementationPhase(*args, **kwargs)
            phase.deadline = time.monotonic() - 1
            return phase
        with patch('orchestra.ImplementationPhase', side_effect=expired_phase):
            await self.app.run('director', [], scheduler.shared_budget, {}, self.trace,
                               self.task, scheduler=scheduler, mutation_requested=True,
                               finalization_deadline=time.monotonic() + 10)
        self.assertEqual(counts['director'], 3)  # wait, explicit blocker, final
        self.assertEqual(counts['worker'], 1)
        self.assertTrue(all(r['status'] == 'completed' for r in scheduler.tasks.values()))

    async def test_any_ignores_previously_collected_default_task(self):
        scheduler = self.scheduler()
        release = asyncio.Event()
        async def work(*args, **kwargs):
            if kwargs['label'] == 'worker-2':
                await release.wait()
            return {'answer': 'Done.', 'status': 'completed'}
        self.app.run = AsyncMock(side_effect=work)
        scheduler.submit('worker-1', 'First.', self.task, 2, 500)
        await scheduler.wait(until_event=True)
        second = scheduler.submit('worker-2', 'Second.', self.task, 2, 500)
        waiter = asyncio.create_task(scheduler.wait(return_when='any', until_event=True))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        release.set()
        result = await waiter
        self.assertEqual([r['task_id'] for r in result['tasks']], [second['task_id']])
        self.assertFalse(result['timed_out'])

    async def test_deadline_returns_pending_without_cancelling_worker(self):
        scheduler = self.scheduler()
        async def work(*args, **kwargs):
            await asyncio.Event().wait()
        self.app.run = AsyncMock(side_effect=work)
        scheduler.submit('worker-1', 'Pending.', self.task, 2, 500)
        try:
            result = await scheduler.wait(until_event=True, deadline=time.monotonic() + .02)
            self.assertTrue(result['timed_out'])
            self.assertEqual(result['tasks'][0]['status'], 'running')
        finally:
            await scheduler.cancel_all()

    async def test_waiter_cancellation_propagates_without_cancelling_worker(self):
        scheduler = self.scheduler()
        async def work(*args, **kwargs):
            await asyncio.Event().wait()
        self.app.run = AsyncMock(side_effect=work)
        scheduler.submit('worker-1', 'Pending.', self.task, 2, 500)
        waiter = asyncio.create_task(scheduler.wait(until_event=True))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertTrue(all(not r['asyncio_task'].done() for r in scheduler.tasks.values()))
        await scheduler.cancel_all()

    async def test_runtime_distinguishes_call_budget_from_deadline(self):
        for expired, expected in [(False, 'call_budget'), (True, 'finalization_deadline')]:
            with self.subTest(expected=expected):
                self.app.request = AsyncMock(return_value={'role': 'assistant', 'content': 'Partial.'})
                await self.app.run('director', [], {'calls': 1}, {}, self.trace,
                    self.task, finalization_deadline=time.monotonic() + (-1 if expired else 60))
                resource = self.app.request.call_args.args[3]
                self.assertIn(f'finalization reason={expected}', resource)
                events = [e for e in self.trace.events if e['event'] == 'finalization_requested']
                self.assertEqual(events[-1]['reason'], expected)
