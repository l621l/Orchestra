import unittest
from unittest.mock import AsyncMock, patch

from orchestra_core.runtime.stage_budget import allowance, request_budget
from orchestra_core.models.model_errors import TaskCostLimit
from tests import test_direct_core as fixtures


class BudgetMath(unittest.TestCase):
    def test_reserve_and_disabled_review(self):
        options = {'max_task_cost_usd': 1, 'completion_review_budget_percent': 20}
        self.assertEqual(allowance(options, True, False), 1)
        self.assertEqual(allowance(options, True, True), .2)
        self.assertEqual(allowance(options, False, False), 1)

    def test_output_clamps_to_remaining_money(self):
        cap = request_budget(.02, {'input_cache_miss': 2, 'output': 10}, [], [], 8192)
        self.assertLess(cap, 2000)

    def test_budget_requires_input_and_minimum_output(self):
        rates = {'input_cache_miss': 2, 'output': 10}
        with self.assertRaises(TaskCostLimit):
            request_budget(.001, rates, ['x' * 10000], [], 8192)
        with self.assertRaises(TaskCostLimit):
            request_budget(.002, rates, [], [], 8192)
        self.assertEqual(request_budget(.002, rates, [], [], 128), 128)

    def test_zero_output_price_still_counts_input(self):
        cap = request_budget(1, {'input_cache_miss': 2, 'output': 0}, [], [], 8192)
        self.assertEqual(cap, 8192)
        with self.assertRaises(TaskCostLimit):
            request_budget(0, {'input_cache_miss': 2, 'output': 0}, [], [], 8192)


class StageBudgetIntegration(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.DirectCore.setUp

    async def test_unreported_usage_cannot_lead_to_free_repeated_requests(self):
        app = self.app
        app.options.update(max_task_cost_usd=1, completion_review_enabled=True)
        app.load_or_start('Build code')

        async def unmetered(engine, role, *args, **kwargs):
            engine.begin_usage(args[3], role, role, 'mimo', 'mimo-v2.6-pro')
            return {'role': 'assistant', 'content': 'response'}

        with patch('orchestra_core.models.writer_transport.request', side_effect=unmetered) as transport:
            await app.request_executor('writer', 'continuous_work', [], [], '', {},
                                       self.trace, self.parent, False, max_output_tokens=8192)
            self.assertTrue(app._state['budget_estimated'])
            self.assertEqual(app.stage_budget(), ('implement', 1, 1))
            with self.assertRaises(TaskCostLimit):
                await app.request_executor('writer', 'continuous_work', [], [], '', {},
                                           self.trace, self.parent, False, max_output_tokens=8192)
            self.assertEqual(transport.await_count, 1)

    async def test_reviewer_charges_own_stage_without_money_guidance(self):
        app = self.app
        app.options.update(max_task_cost_usd=1, completion_review_enabled=True)
        app.load_or_start('Review code')
        app._state.update(budget_spent=.79, stage_spent={'implement': .79, 'review': 0})
        app._review_active = True
        usage = {}

        async def reply(engine, role, messages, tools, status, usage, *args, **kwargs):
            self.assertNotIn('STAGE MONEY BUDGET', status)
            self.assertNotIn('$0.20000', status)
            entry = engine.begin_usage(usage, kwargs['label'], role, 'mimo', 'mimo-v2.6-pro')
            engine.add_usage(entry, {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}, None)
            return {'role': 'assistant', 'content': 'done'}

        with patch('orchestra_core.models.writer_transport.request', side_effect=reply):
            await app.request_executor('writer', 'completion_review', [], [], '', usage,
                                       self.trace, self.parent, False, max_output_tokens=8192)
        self.assertEqual(app._state['stage_spent']['implement'], .79)
        self.assertEqual(set(usage), {'reviewer'})
        self.assertGreater(app._state['stage_spent']['review'], 0)
        self.assertAlmostEqual(app._state['budget_spent'], .79 + app._state['stage_spent']['review'])

    async def test_implementation_can_use_review_share(self):
        app = self.app
        app.options.update(max_task_cost_usd=1, completion_review_enabled=True)
        app.load_or_start('Build code')
        app._state.update(budget_spent=.95, stage_spent={'implement': .95, 'review': 0})
        with patch('orchestra_core.models.writer_transport.request', new_callable=AsyncMock) as transport:
            await app.request_executor('writer', 'continuous_work', [], [], '', {},
                                       self.trace, self.parent, False, max_output_tokens=8192)
            transport.assert_awaited_once()
        app._review_active = True
        self.assertAlmostEqual(app.stage_budget()[1], .05)
