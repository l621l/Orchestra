import json
import unittest
from unittest.mock import AsyncMock

from tests.test_single_model import SingleModel


class CompletionReview(unittest.IsolatedAsyncioTestCase):
    setUp = SingleModel.setUp
    call = SingleModel.call
    response = SingleModel.response

    def complete(self):
        return {'role': 'assistant', 'content': json.dumps({
            'summary': 'Done', 'done': True, 'state': 'complete', 'next_steps': ''})}

    async def execute(self, replies, budget=12, task='Implement the requested file'):
        self.app.direct_mode = True
        self.app.requires_launch_verification = lambda: False
        self.app.request = AsyncMock(side_effect=replies)
        messages = [{'role': 'user', 'content': task}]
        result = await self.app.run('worker', messages, {'calls': budget}, {}, self.trace, self.task)
        return result, messages

    async def test_one_extra_request_without_repeated_review(self):
        result, messages = await self.execute([
            self.response(self.call('create_file', path='value.txt', content='correct')),
            self.response(self.call('run_check', name='unit')),
            self.complete(), self.complete(),
        ])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.app.request.await_count, 4)
        self.assertEqual(sum('ONE-TIME COMPLETION REVIEW' in (m.get('content') or '') for m in messages), 1)
        self.assertEqual(self.app.request.call_args_list[-1].kwargs['max_output_tokens'], 2048)

    async def test_review_can_reproduce_repair_and_verify(self):
        result, _ = await self.execute([
            self.response(self.call('create_file', path='value.txt', content='wrong')),
            self.complete(),
            self.response(self.call('run_check', name='unit')),
            self.response(self.call('replace_text', path='value.txt', old_text='wrong', new_text='correct')),
            self.response(self.call('run_check', name='unit')),
            self.complete(),
        ])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual((self.root/'value.txt').read_text(), 'correct')
        self.assertEqual(self.app.request.await_count, 6)
        self.assertTrue(self.app.request.call_args_list[-1].args[8])

    async def test_review_budget_stops_incomplete_work(self):
        continuing = {'role': 'assistant', 'content': json.dumps({
            'summary': 'Unresolved defect', 'done': False, 'state': 'continue', 'next_steps': 'Fix it'})}
        result, _ = await self.execute([
            self.response(self.call('create_file', path='value.txt', content='wrong')),
            self.complete(),
            self.response(self.call('read_file', path='value.txt')),
            self.response(self.call('read_file', path='value.txt')),
            self.response(self.call('read_file', path='value.txt')),
            continuing,
        ])
        self.assertEqual(self.app.request.await_count, 6)
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(json.loads(result['answer'])['done'])

    async def test_no_budget_does_not_claim_reviewed_completion(self):
        result, _ = await self.execute([
            self.response(self.call('create_file', path='value.txt', content='correct')),
            self.complete(),
        ], budget=2)
        self.assertEqual(result['stop_reason'], 'completion_review_pending')
        self.assertFalse(json.loads(result['answer'])['done'])

    async def test_read_only_and_opt_out_do_not_add_requests(self):
        result, _ = await self.execute([self.complete()], task='Explain the code')
        self.assertEqual(self.app.request.await_count, 1)
        self.settings['execution']['completion_review_enabled'] = False
        await self.execute([
            self.response(self.call('create_file', path='value.txt', content='correct')),
            self.complete(),
        ])
        self.assertEqual(self.app.request.await_count, 2)
