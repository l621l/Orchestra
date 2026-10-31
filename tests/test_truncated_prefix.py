import json
import unittest
from unittest.mock import AsyncMock
from orchestra import OutputLimitError, complete_tool_prefix
from tests import test_single_model as fixtures


class TruncatedPrefix(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.SingleModel.setUp
    call=fixtures.SingleModel.call
    response=fixtures.SingleModel.response
    run_script=fixtures.SingleModel.run_script

    def unfinished(self):
        call=self.call('create_file',path='broken.py',content='unfinished')
        call['id']='broken'
        call['function']['arguments']='{"path":"broken.py","content":"'
        return call

    async def test_valid_prefix_executes_once_and_pairs_remain_valid(self):
        first=self.call('create_file',path='value.txt',content='correct')
        first['id']='first'
        result,outputs,pool=await self.run_script([
            OutputLimitError('length',tool_drafts=[first,self.unfinished()]),
            self.response(self.call('run_check',name='unit')),
            {'role':'assistant','content':'Done'}],budget=6)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(pool['calls'],3)
        self.assertTrue(outputs[0]['ok'])
        self.assertEqual((self.root/'value.txt').read_text(),'correct')
        self.assertFalse((self.root/'broken.py').exists())
        messages=self.app.request.call_args_list[-1].args[1]
        ids=[c['id'] for m in messages for c in m.get('tool_calls',[])]
        results=[m['tool_call_id'] for m in messages if m.get('role')=='tool']
        self.assertEqual(ids,results)
        self.assertEqual(ids.count('first'),1)
        self.assertNotIn('broken',ids)

    async def test_valid_call_after_broken_call_is_not_executed(self):
        later=self.call('create_file',path='later.py',content='bad')
        result,_,_=await self.run_script([
            OutputLimitError('length',tool_drafts=[self.unfinished(),later]),
            {'role':'assistant','content':'Partial'}],budget=4)
        self.assertFalse((self.root/'later.py').exists())

    async def test_prefix_failure_prevents_dependent_actions(self):
        (self.root/'value.txt').write_text('existing')
        first=self.call('create_file',path='value.txt',content='correct');first['id']='first'
        second=self.call('create_file',path='dependent.py',content='should not execute');second['id']='second'
        _,outputs,_=await self.run_script([
            OutputLimitError('length',tool_drafts=[first,second,self.unfinished()]),
            {'role':'assistant','content':'Partial'}],budget=4)
        self.assertFalse(outputs[0]['ok'])
        self.assertEqual(outputs[1]['error'],'skipped_after_prefix_failure')
        self.assertFalse((self.root/'dependent.py').exists())

    async def test_output_exhaustion_still_preserves_complete_work_without_another_request(self):
        usage={}
        first=self.call('create_file',path='value.txt',content='correct')
        async def request(*args,**kwargs):
            usage['director']={'output_budget_tokens':20000}
            raise OutputLimitError('length',tool_drafts=[first,self.unfinished()])
        self.app.request=AsyncMock(side_effect=request)
        result=await self.app.run('director',[],{'calls':100},usage,self.trace,self.task)
        self.assertEqual(self.app.request.await_count,1)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['modified_files'],['value.txt'])
        self.assertEqual((self.root/'value.txt').read_text(),'correct')

    def test_schema_errors_duplicate_ids_and_git_mutations_are_not_salvaged(self):
        definitions={t['function']['name']:t for t in self.app.available_tools()}
        valid=self.call('create_file',path='a.py',content='ok')
        invalid=self.call('create_file',path='b.py',content='bad');invalid['function']['arguments']='{"path":"b.py"}'
        self.assertEqual(len(complete_tool_prefix([valid,invalid],definitions)),1)
        self.assertEqual(len(complete_tool_prefix([valid,valid],definitions)),1)
        self.assertEqual(complete_tool_prefix([valid],definitions,{valid['id']}),[])
        self.assertEqual(complete_tool_prefix([valid,self.call('create_commit',message='x')],definitions),[])
