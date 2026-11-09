import json
import unittest
from unittest.mock import AsyncMock
from tests import test_single_model as fixtures


class Iterations(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SingleModel.setUp
    call = fixtures.SingleModel.call
    response = fixtures.SingleModel.response

    async def test_five_fresh_contexts_only_previous_handoff_and_original_task(self):
        self.settings['execution']['stop_after_confirmation'] = False
        calls = []
        async def iteration(role, messages, budget, usage, trace, task_id, **kwargs):
            index = len(calls)+1
            calls.append((messages, dict(budget), kwargs))
            budget['calls'] -= 4
            return {'answer':json.dumps({'summary':f'progress-{index}',
                'next_steps':f'next-{index}','done':index==5}),
                'status':'completed','modified_files':[f'file-{index}.py'],
                'verification':{'status':'passed','revision':1,'checks':{}}}
        self.app.run = AsyncMock(side_effect=iteration)
        result = await self.app.run_iterations('original task', {}, self.trace, self.task)
        self.assertEqual(len(calls),5)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['modified_files']),5)
        for index, (messages, budget, kwargs) in enumerate(calls):
            self.assertEqual(messages[1]['content'],'original task')
            self.assertEqual(budget['calls'],self.settings['execution']['max_api_calls_per_turn'])
            self.assertEqual(len(messages),3 if index==0 else 4)
            if index:
                self.assertIn(f'progress-{index}',messages[2]['content'])
                self.assertNotIn('tool_calls',messages[2]['content'])
            self.assertTrue((self.trace.directory/f'iteration_{index+1}.json').exists())

    async def test_each_iteration_has_exactly_one_thinking_request(self):
        self.app.request = AsyncMock(side_effect=[
            self.response(self.call('read_file',path='settings.txt')),
            self.response(self.call('create_file',path='value.txt',content='correct')),
            self.response(self.call('run_check',name='unit')),
            {'role':'assistant','content':'Checked'}])
        (self.root/'settings.txt').write_text('input')
        result = await self.app.run('worker',[],{'calls':8},{},self.trace,self.task)
        self.assertEqual(result['verification']['status'],'passed')

    async def test_bad_handoff_is_bounded_and_does_not_claim_done(self):
        self.settings['execution']['iterations_per_task']=2
        self.app.run = AsyncMock(return_value={'answer':'x'*12000,'status':'completed',
            'modified_files':[], 'verification':{'status':'passed'}})
        result = await self.app.run_iterations('original',{},self.trace,self.task)
        self.assertEqual(result['status'],'partial')
        messages = self.app.run.call_args_list[1].args[1]
        self.assertLess(len(messages[2]['content']),5000)
        self.assertTrue(any(e['event']=='handoff_format_fallback' for e in self.trace.events))

    async def test_api_failure_stops_chain(self):
        self.app.run = AsyncMock(side_effect=RuntimeError('failure'))
        with self.assertRaises(RuntimeError):
            await self.app.run_iterations('original',{},self.trace,self.task)
        self.assertEqual(self.app.run.call_count,1)

    async def test_completion_requires_one_unchanged_confirming_iteration(self):
        report = {'answer':json.dumps({'summary':'done','next_steps':'none','done':True}),
                  'status':'completed','modified_files':[], 'verification':{'status':'passed'}}
        self.app.run = AsyncMock(return_value=report)
        result = await self.app.run_iterations('original',{},self.trace,self.task)
        self.assertEqual(self.app.run.call_count,2)
        self.assertTrue(result['completion_confirmed'])
        self.assertEqual(result['iterations'],2)
        self.assertEqual(result['status'],'completed')
        self.assertIn('CONFIRMATION ITERATION',self.app.run.call_args.args[1][-1]['content'])

    async def test_changes_or_failure_prevent_early_confirmation(self):
        def report(done, files, passed=True):
            return {'answer':json.dumps({'summary':'progress','next_steps':'check','done':done}),
                    'status':'completed' if passed else 'partial','modified_files':files,
                    'verification':{'status':'passed' if passed else 'failed'}}
        self.app.run = AsyncMock(side_effect=[report(True,['first.py']),report(True,['fix.py']),
            report(False,[],False),report(True,[]),report(True,[])])
        result = await self.app.run_iterations('original',{},self.trace,self.task)
        self.assertEqual(self.app.run.call_count,5)
        self.assertTrue(result['completion_confirmed'])

    async def test_done_on_last_iteration_is_still_unconfirmed(self):
        self.settings['execution']['iterations_per_task']=1
        self.app.run = AsyncMock(return_value={'answer':json.dumps(
            {'summary':'done','next_steps':'none','done':True}), 'status':'completed',
            'modified_files':[], 'verification':{'status':'passed'}})
        result = await self.app.run_iterations('original',{},self.trace,self.task)
        self.assertFalse(result['completion_confirmed'])
        self.assertEqual(result['status'],'partial')

    async def test_check_schema_and_prompt_expose_actual_configuration(self):
        tool = next(t for t in self.app.available_tools() if t['function']['name']=='run_check')
        self.assertEqual(tool['function']['parameters']['properties']['name']['enum'],['unit'])
        self.app.request = AsyncMock(return_value={'role':'assistant','content':'Report'})
        await self.app.run('worker',[],{'calls':4},{},self.trace,self.task)
        self.assertIn('assert Path',self.app.request.call_args.args[3])
