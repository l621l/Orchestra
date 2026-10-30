import json
import unittest
from unittest.mock import AsyncMock
from orchestra import is_internal_report
from tests import test_single_model as fixtures


class CompletionGuard(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SingleModel.setUp
    call = fixtures.SingleModel.call
    response = fixtures.SingleModel.response

    async def execute(self, replies, task='fix the bug', budget=8):
        self.app.request = AsyncMock(side_effect=replies)
        messages = [{'role':'user','content':task}]
        pool = {'calls':budget}
        result = await self.app.run('director', messages, pool, {}, self.trace, self.task)
        return result, pool

    async def test_research_only_answer_is_repaired_with_real_edit_and_check(self):
        result, pool = await self.execute([
            {'role':'assistant','content':'I inspected it.'},
            self.response(self.call('create_file',path='value.txt',content='correct')),
            self.response(self.call('run_check',name='unit')),
            {'role':'assistant','content':'Fixed and checked.'}])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['modified_files'],['value.txt'])
        self.assertEqual(pool['calls'],4)

    async def test_internal_echo_exhausts_bounded_repairs_without_persisting_echo(self):
        echo='{"execution_record": []}]'
        result, pool = await self.execute([{'role':'assistant','content':echo}]*3)
        self.assertEqual(result['status'],'partial')
        self.assertFalse(is_internal_report(result['answer']))
        self.assertEqual(pool['calls'],5)

    async def test_final_call_is_not_retried(self):
        result, pool = await self.execute([{'role':'assistant','content':'Research only'}],budget=1)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(pool['calls'],0)

    async def test_read_only_request_can_finish(self):
        result, _ = await self.execute([{'role':'assistant','content':'Here is the analysis.'}],task='Explain this code')
        self.assertEqual(result['status'],'completed')

    async def test_actionable_blocker_can_finish(self):
        answer=json.dumps({'state':'needs_user','blocker':{'action':'Provide asset','reason':'Required asset missing','evidence':'assets directory is empty'}})
        result, _ = await self.execute([{'role':'assistant','content':answer}])
        self.assertEqual(result['answer'],answer)

    def test_malformed_and_nested_history_are_detected(self):
        self.assertTrue(is_internal_report('{"execution_record": []}]'))
        self.assertTrue(is_internal_report(json.dumps({'summary':'{"program_history_record": true}'})))

    async def test_diagnosis_and_next_step_survive_forced_partial(self):
        answer=json.dumps({'summary':'Bullet origin is coupled to player motion.',
                           'next_steps':'Reproduce shoot during ascent.', 'done':True})
        result, _ = await self.execute([{'role':'assistant','content':answer}],budget=1)
        report=json.loads(result['answer'])
        self.assertIn('Bullet origin',report['summary'])
        self.assertIn('unverified',report['summary'])
        self.assertEqual(report['next_steps'],'Reproduce shoot during ascent.')
        self.assertFalse(report['done'])

    async def test_checkpoint_arrives_before_budget_is_spent(self):
        snapshots=[]
        replies=iter([self.response(self.call('list_files',path='.')) for _ in range(4)]+[
            self.response(self.call('create_file',path='value.txt',content='correct')),
            self.response(self.call('run_check',name='unit')),
            {'role':'assistant','content':'Fixed'}])
        async def request(role,messages,*args,**kwargs):
            snapshots.append([m.get('content') or '' for m in messages])
            return next(replies)
        self.app.request=request
        pool={'calls':8}
        result=await self.app.run('director',[{'role':'user','content':'fix the bug'}],pool,{},self.trace,self.task)
        self.assertFalse(any('IMPLEMENTATION CHECKPOINT' in m for m in snapshots[3]))
        self.assertTrue(any('IMPLEMENTATION CHECKPOINT' in m for m in snapshots[4]))
        self.assertEqual(result['verification']['status'],'passed')
        self.assertEqual(pool['calls'],1)

    def progress_report(self):
        return {'role':'assistant','content':json.dumps({'summary':'Milestone delivered',
            'next_steps':'Implement the next requirement','done':False,'state':'continue','blocker':None})}

    async def test_continue_after_verified_milestone_resumes_and_verifies_new_revision(self):
        result,pool=await self.execute([
            self.response(self.call('create_file',path='value.txt',content='wrong')),
            self.progress_report(),
            self.response(self.call('replace_text',path='value.txt',old_text='wrong',new_text='correct')),
            self.response(self.call('run_check',name='unit')),
            {'role':'assistant','content':json.dumps({'summary':'Done','next_steps':'','done':True,'state':'complete'})}
        ],budget=8)
        self.assertEqual(self.app.request.await_count,5)
        self.assertEqual(pool['calls'],3)
        self.assertEqual(result['verification']['status'],'passed')
        self.assertEqual(result['verification']['revision'],2)
        self.assertTrue(any(e['event']=='continuation_report' and e['resumed'] for e in self.trace.events))

    async def test_repeated_continue_reports_stop_without_losing_next_steps(self):
        result,pool=await self.execute([self.progress_report() for _ in range(3)],budget=8)
        self.assertEqual(pool['calls'],5)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['stop_reason'],'continuation_stalled')
        self.assertEqual(json.loads(result['answer'])['next_steps'],'Implement the next requirement')

    async def test_continue_on_last_call_does_not_restart_budget(self):
        result,pool=await self.execute([self.progress_report()],budget=1)
        self.assertEqual(pool['calls'],0)
        self.assertEqual(self.app.request.await_count,1)
        self.assertEqual(result['status'],'partial')

    async def test_continue_does_not_bypass_output_budget(self):
        self.settings['execution']['max_output_tokens_per_turn']=20000
        usage={}
        async def request(*args,**kwargs):
            usage['director']={'output_budget_tokens':20000}
            return self.progress_report()
        self.app.request=request
        result=await self.app.run('director',[],{'calls':100},usage,self.trace,self.task)
        self.assertEqual(result['stop_reason'],'output_budget')
        self.assertEqual(result['status'],'partial')
