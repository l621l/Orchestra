import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from orchestra import OutputLimitError, ModelResponseError
from tests import test_single_model as fixtures


class OutputRecovery(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SingleModel.setUp
    call = fixtures.SingleModel.call
    response = fixtures.SingleModel.response
    run_script = fixtures.SingleModel.run_script

    async def test_length_then_edit_check_uses_existing_budget(self):
        result, outputs, pool = await self.run_script([
            OutputLimitError('Output token limit reached'),
            self.response(self.call('create_file', path='value.txt', content='correct')),
            self.response(self.call('run_check', name='unit')),
            {'role':'assistant','content':'Implemented and checked.'}], budget=8)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(pool['calls'], 4)
        self.assertEqual(result['verification']['status'], 'passed')
        expected_cap = self.settings['generation']['max_output_tokens']
        self.assertEqual(self.app.request.call_args_list[1].kwargs['max_output_tokens'], expected_cap)
        self.assertEqual(sum(e['event']=='output_limit_recovery_started' for e in self.trace.events),1)
        self.assertIsNone(self.trace.error_message)

    async def test_repeated_length_stops_and_keeps_existing_files_and_draft(self):
        result, outputs, pool = await self.run_script([
            self.response(self.call('create_file', path='value.txt', content='correct')),
            OutputLimitError('length', draft='Some work is saved.'),
            OutputLimitError('length')], budget=3)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['stop_reason'], 'output_limit')
        self.assertIn('Some work is saved.', result['answer'])
        self.assertEqual((self.root/'value.txt').read_text(), 'correct')
        self.assertEqual(self.app.request.call_count, 3)
        self.assertEqual(pool['calls'], 0)

    async def test_no_call_budget_returns_partial(self):
        result, _, pool = await self.run_script([OutputLimitError('length')], budget=1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.app.request.call_count, 1)
        self.assertEqual(pool['calls'], 0)

    async def test_two_truncations_do_not_stop_work_with_budget_remaining(self):
        result, outputs, pool = await self.run_script([
            OutputLimitError('length'), OutputLimitError('length'),
            self.response(self.call('create_file',path='value.txt',content='correct')),
            self.response(self.call('run_check',name='unit')),
            {'role':'assistant','content':'Fixed and checked'}],budget=8)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(pool['calls'],3)
        self.assertEqual((self.root/'value.txt').read_text(),'correct')
        self.assertEqual(sum(e['event']=='output_limit_recovery_started' for e in self.trace.events),2)


    async def test_other_protocol_failure_is_not_retried(self):
        with self.assertRaises(ModelResponseError):
            await self.run_script([ModelResponseError('content_filter')])
        self.assertEqual(self.app.request.call_count,1)
