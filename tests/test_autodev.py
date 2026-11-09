import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from autodev import AutonomousPipeline
from autodev_tools import run_command
from orchestra import TurnTrace, ModelResponseError
from tool_errors import ToolError

BASE=Path(__file__).resolve().parents[1]

def task(key='S1',goal='Create runnable program',files=None,depends=None):
    return {'id':key,'goal':goal,'acceptance':['The program runs and prints hello'],
            'files':files or ['main.py'],'depends_on':depends or []}

class AutoDevelopment(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        settings=json.loads((BASE/'settings.json').read_text(encoding='utf-8'))
        config={'providers':{'mimo':{},'gpt':{}},'roles':{
            'writer':{'provider':'mimo','model':'mimo-v2.6-pro'},
            'strong':{'provider':'gpt','model':'gpt-6.1-sol'}}}
        with patch('orchestra.BASE',self.root):
            self.app=AutonomousPipeline(config,self.root,settings,json.loads((BASE/'prompts.json').read_text(encoding='utf-8')),json.loads((BASE/'pricing.json').read_text(encoding='utf-8')))
        self.app.dashboard=MagicMock()
        self.trace=TurnTrace(self.root/'logs','auto',self.root)
        self.parent=self.trace.start_task('worker','auto')
        self.app.load_or_start('Create a runnable program printing hello.')

    def call(self,name,**args):
        return {'role':'assistant','content':None,'tool_calls':[{'id':'call','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}

    def set_plan(self,tasks=None):
        return self.app.execute('set_plan',{'tasks':tasks or [task()]})

    def test_invalid_python_edit_never_changes_source(self):
        self.set_plan()
        self.app.execute('create_file',{'path':'main.py','content':'print("hello")\n'})
        with self.assertRaises(ToolError) as error:
            self.app.execute('replace_text',{'path':'main.py','old_text':'print("hello")','new_text':'if True:\nprint("broken")'})
        self.assertEqual(error.exception.code,'invalid_python')
        self.assertEqual((self.root/'main.py').read_text(),'print("hello")\n')
        with self.assertRaises(ToolError):
            self.app.execute('create_file',{'path':'bad.py','content':'def x(\n'})
        self.assertFalse((self.root/'bad.py').exists())

    def test_finish_needs_passed_command_after_latest_edit(self):
        self.set_plan()
        self.app.execute('create_file',{'path':'main.py','content':'print("hello")\n'})
        with self.assertRaises(ToolError):self.app.execute('finish_task',{'id':'S1','summary':'done'})
        check=self.app.execute('run_command',{'argv':['{python}','main.py'],'purpose':'verify','criteria':[1]})
        self.assertTrue(check['passed'])
        self.app.execute('replace_text',{'path':'main.py','old_text':'hello','new_text':'hello world'})
        with self.assertRaises(ToolError):self.app.execute('finish_task',{'id':'S1','summary':'done'})
        self.app.execute('run_command',{'argv':['{python}','main.py'],'purpose':'verify','criteria':[1]})
        self.app.execute('finish_task',{'id':'S1','summary':'runs'})
        self.assertIsNone(self.app.current_task())

    async def test_two_real_tasks_advance_automatically(self):
        scripts=[self.call('set_plan',tasks=[task(),task('S2','Extend output',depends=['S1'])]),
                 self.call('create_file',path='main.py',content='print("hello")\n'),
                 self.call('run_command',argv=['{python}','main.py'],purpose='verify',criteria=[1]),
                 self.call('finish_task',id='S1',summary='Created runnable program'),
                 self.call('replace_text',path='main.py',old_text='hello',new_text='hello world'),
                 self.call('run_command',argv=['{python}','main.py'],purpose='verify',criteria=[1]),
                 self.call('finish_task',id='S2',summary='Extended output')]
        self.app.request=AsyncMock(side_effect=scripts)
        result=await self.app.run_user_task('Create hello then extend it.',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        self.assertEqual((self.root/'main.py').read_text(),'print("hello world")\n')
        self.assertEqual([t['status'] for t in self.app._state['plan']],['done','done'])
        self.assertEqual(self.app.request.await_count,7)

    async def test_loop_stops_after_bounded_recovery(self):
        self.app.request=AsyncMock(return_value=self.call('list_files',path='.'))
        result=await self.app.run_user_task('Build an app.',{},self.trace,self.parent)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(self.app._state['stop_reason'],'planning_not_completed')
        self.assertLessEqual(self.app.request.await_count,4)
        self.assertTrue(any(e['event']=='planning_required' for e in self.trace.events))

    async def test_plain_done_cannot_complete_unimplemented_scope(self):
        self.app.request=AsyncMock(return_value={'role':'assistant','content':'{"done":true,"state":"complete"}'})
        result=await self.app.run_user_task('Build an app.',{},self.trace,self.parent)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(self.app._state['plan'],[])

    def test_cost_guard_blocks_before_transport_and_persists_across_continue(self):
        self.app.options['max_task_cost_usd']=.01
        payload={'model':'gpt-6.1-sol','input':[{'role':'user','content':'task'}],'max_output_tokens':4096}
        with self.assertRaisesRegex(ModelResponseError,'cost limit'):
            self.app.validate_request_input('strong',payload)
        self.assertEqual(self.app._reservation,0)
        self.app._state['budget_spent']=.009
        self.app.save_state();self.app.load_or_start('продолжи')
        self.assertEqual(self.app._state['budget_spent'],.009)

    def test_generic_runtime_failure_and_timeout_are_not_success(self):
        result=run_command(self.app.repository,['{python}','-c','raise RuntimeError("boom")'])
        self.assertFalse(result['passed']);self.assertIn('boom',result['output'])
        with self.assertRaises(ToolError):run_command(self.app.repository,['{python}','-c','import time;time.sleep(4)'],timeout_seconds=1)
        with self.assertRaises(ToolError):run_command(self.app.repository,['git','status'])

    def test_failed_later_command_invalidates_previous_success(self):
        self.set_plan()
        self.app.execute('create_file',{'path':'main.py','content':'print("hello")'})
        self.app.execute('run_command',{'argv':['{python}','main.py'],'purpose':'verify','criteria':[1]})
        with self.assertRaises(ToolError):
            self.app.execute('run_command',{'argv':['{python}','-c','import time;time.sleep(3)'],'timeout_seconds':1})
        with self.assertRaises(ToolError):
            self.app.execute('finish_task',{'id':'S1','summary':'not actually checked'})

    async def test_cost_rejection_does_not_advance_actual_model_cycle(self):
        self.app.options['max_task_cost_usd']=.000001
        self.app._position=2
        with self.assertRaisesRegex(ModelResponseError,'cost limit'):
            await self.app.request('worker',self.app.context(12000),self.app.available_tools(),
                                   'budget',{},self.trace,self.parent)
        self.assertEqual(self.app._position,2)

    def test_continue_grants_calls_but_preserves_dollar_limit(self):
        self.app._state.update(task_calls=24,budget_spent=.8)
        self.app.save_state();self.app.load_or_start('продолжи')
        self.assertEqual(self.app._state['task_calls'],0)
        self.assertEqual(self.app._state['budget_spent'],.8)

    def test_resume_reopens_tasks_after_external_deletion(self):
        self.set_plan();self.app.execute('create_file',{'path':'main.py','content':'print("hello")'})
        self.app.execute('run_command',{'argv':['{python}','main.py'],'purpose':'verify','criteria':[1]})
        self.app.execute('finish_task',{'id':'S1','summary':'runs'})
        self.app.save_state();(self.root/'main.py').unlink()
        self.app.load_or_start('продолжи')
        self.assertIsNotNone(self.app.current_task())
        self.assertIsNone(self.app._state['last_check'])

    def test_bounded_context_carries_current_task_and_recent_error(self):
        self.set_plan()
        self.app._state['recent']=[{'tool':'read_file','args':{'path':'big.py'},'result':{'content':'x'*100000}}]
        self.app._state['feedback']='Fix syntax at line 14'
        for limit in (6000,12000):
            messages=self.app.context(limit)
            self.assertLessEqual(len(messages[1]['content']),limit)
            packet=json.loads(messages[1]['content'])
            self.assertEqual(packet['current_task']['id'],'S1')
            self.assertIn('Fix syntax',packet['feedback'])

    def test_no_git_or_legacy_test_tool_exposed(self):
        self.set_plan()
        names={t['function']['name'] for t in self.app.available_tools()}
        self.assertTrue({'set_plan','finish_task','read_spec','run_command','replace_text'}<=names)
        self.assertFalse({'git_status','apply_patch','run_check'}&names)

    def test_recovery_can_split_work_without_dropping_scope(self):
        self.set_plan()
        self.app._state['recovery']=1
        sub=task('S0','Create the command-line entry point')
        original=task(depends=['S0'])
        self.app.execute('set_plan',{'tasks':[sub,original]})
        self.assertEqual(self.app.current_task()['id'],'S0')
        self.app._state['recovery']=1
        with self.assertRaises(ToolError):
            self.app.execute('set_plan',{'tasks':[sub]})

    async def test_actual_alternation_in_task_controller(self):
        observed=[]
        scripts=[self.call('set_plan',tasks=[task()]),
            self.call('create_file',path='main.py',content='print("hello")'),
            self.call('read_file',path='main.py'),
            self.call('run_command',argv=['{python}','main.py'],purpose='verify',criteria=[1]),
            self.call('list_files',path='.'),
            self.call('finish_task',id='S1',summary='Runs')]
        async def respond(app,role,*args,**kwargs):
            observed.append(role)
            config=app.role_settings(role)
            app.begin_usage(args[3],role,role,config['provider'],config['model'])
            return scripts[len(observed)-1]
        with patch('writer_transport.request',side_effect=respond),patch('responses_transport.request',side_effect=respond):
            result=await self.app.run_user_task('Build program',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(observed,['writer']*5+['strong'])
