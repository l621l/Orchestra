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
        self.assertEqual(self.app.request.call_args_list[1].kwargs['thinking_override'], 'disabled')
        self.assertEqual(self.app.request.call_args_list[1].kwargs['max_completion_tokens'],self.settings['execution']['recovery_max_completion_tokens'])
        self.assertTrue(all(call.kwargs['thinking_override'] == 'disabled'
                            for call in self.app.request.call_args_list[1:]))
        self.assertEqual(sum(e['event']=='output_limit_recovery_started' for e in self.trace.events),1)
        self.assertIsNone(self.trace.error_message)

    async def test_next_user_turn_does_not_inherit_thinking_override(self):
        await self.run_script([OutputLimitError('length'),
            {'role':'assistant','content':'Partial report.'}])
        await self.run_script([{'role':'assistant','content':'New task.'}])
        self.assertIsNone(self.app.request.call_args.kwargs['thinking_override'])

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

    async def test_truncation_does_not_bypass_shared_output_budget(self):
        usage={}
        async def request(*args,**kwargs):
            usage['director']={'output_budget_tokens':20000}
            raise OutputLimitError('length')
        self.app.request=request
        result=await self.app.run('director',[],{'calls':100},usage,self.trace,self.task)
        self.assertEqual(result['status'],'partial')
        self.assertFalse(any(e['event']=='output_limit_recovery_started' for e in self.trace.events))

    async def test_other_protocol_failure_is_not_retried(self):
        with self.assertRaises(ModelResponseError):
            await self.run_script([ModelResponseError('content_filter')])
        self.assertEqual(self.app.request.call_count,1)

    async def test_actual_truncated_tool_is_discarded_and_mimo_override_sent(self):
        self.app.config['roles']['director']['model'] = 'mimo-v2.6-flash'
        fragment = SimpleNamespace(index=0, id='unfinished', function=SimpleNamespace(
            name='create_file', arguments='{"path":"bad.txt","content":"unfinished'))
        chunk = SimpleNamespace(id='offline-id',model='mimo-v2.6-flash',usage=None,
            choices=[SimpleNamespace(finish_reason='length',delta=SimpleNamespace(
                content=None,reasoning_content='thinking',tool_calls=[fragment]))])
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            def __aiter__(self):
                async def generate(): yield chunk
                return generate()
        create = AsyncMock(return_value=Stream())
        self.app.clients['offline'] = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with self.assertRaises(OutputLimitError):
            await self.app.request('director',[],self.app.available_tools(),'',{},self.trace,self.task,
                                   thinking_override='disabled')
        self.assertEqual(create.call_args.kwargs['extra_body'], {'thinking':{'type':'disabled'}})
        self.assertFalse((self.root/'bad.txt').exists())
        archives = list(self.trace.directory.glob('*_truncated.json'))
        self.assertEqual(len(archives),1)
        self.assertEqual(json.loads(archives[0].read_text())['tool_calls'][0]['function']['arguments'],fragment.function.arguments)
        event = next(e for e in self.trace.events if e['event']=='model_request_finished')
        self.assertEqual(event['finish_reason'], 'length')
        self.assertEqual(event['request']['thinking_override'], 'disabled')
