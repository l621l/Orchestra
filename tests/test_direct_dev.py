import copy,json,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from tests import test_autodev as fixtures
from direct_dev import DirectDevelopment
from orchestra import TurnTrace
from autodev_console import brief

class DirectLoop(unittest.IsolatedAsyncioTestCase):
    call=fixtures.AutoDevelopment.call
    def setUp(self):
        import tempfile
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        base=Path(__file__).resolve().parents[1]
        settings=json.loads((base/'settings.json').read_text())
        config={'providers':{'mimo':{},'gpt':{}},'roles':{'writer':{'provider':'mimo','model':'mimo-v2.6-pro'},'strong':{'provider':'gpt','model':'gpt-6.1-sol'}}}
        with patch('orchestra.BASE',self.root):
            self.app=DirectDevelopment(config,self.root,settings,json.loads((base/'prompts.json').read_text()),json.loads((base/'pricing.json').read_text()))
        from unittest.mock import MagicMock
        self.app.dashboard=MagicMock();self.trace=TurnTrace(self.root/'logs','direct',self.root);self.parent=self.trace.start_task('worker','direct')

    def test_no_planning_gate_or_read_throttle(self):
        self.app.load_or_start('Build program')
        names={t['function']['name'] for t in self.app.available_tools()}
        self.assertFalse({'set_plan','finish_task','remember_work','read_spec','apply_patch','git_status'}&names)
        self.assertIn('replace_text',names)
        source=''.join(f'x{i}={i}\n' for i in range(245))
        self.app.execute_tool('create_file',{'path':'main.py','content':source})
        result=self.app.execute_tool('read_file',{'path':'main.py'})
        self.assertEqual(result['returned_end_line'],200)
        self.assertFalse(self.app.requires_plan_call())

    async def test_first_call_can_write_then_verify_without_protocol(self):
        done={'role':'assistant','content':json.dumps({'summary':'hello works','next_steps':'','done':True,'state':'complete'})}
        self.app.request=AsyncMock(side_effect=[self.call('create_file',path='main.py',content='print("hello")'),self.call('run_command',argv=['{python}','main.py']),done])
        result=await self.app.run_user_task('Create main.py printing hello',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['verification']['status'],'passed')
        calls=self.app.request.call_args_list
        self.assertIs(calls[0].args[1],calls[1].args[1])
        self.assertTrue(any(m['role']=='tool' for m in calls[1].args[1]))
        self.assertEqual((self.root/'main.py').read_text(),'print("hello")')
        self.assertIn('[DONE] Direct',brief(self.app))
        previous=self.app.request.await_count
        cached=await self.app.run_user_task('C',{},self.trace,self.parent)
        self.assertEqual(cached['status'],'completed')
        self.assertEqual(self.app.request.await_count,previous)

    async def test_failed_runtime_does_not_become_success(self):
        done={'role':'assistant','content':json.dumps({'summary':'done','done':True,'state':'complete'})}
        self.app.request=AsyncMock(side_effect=[self.call('create_file',path='main.py',content='raise RuntimeError("broken")'),self.call('run_command',argv=['{python}','main.py']),done,done,done])
        result=await self.app.run_user_task('Create program',{},self.trace,self.parent)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['verification']['status'],'failed')

    async def test_repairs_after_timeout_and_premature_report(self):
        done={'role':'assistant','content':json.dumps({'summary':'done','done':True,'state':'complete'})}
        self.app.request=AsyncMock(side_effect=[
            self.call('create_file',path='main.py',content='raise RuntimeError("broken")'),
            self.call('run_command',argv=['{python}','main.py']),
            TimeoutError(),
            done,
            self.call('replace_text',path='main.py',old_text='raise RuntimeError("broken")',new_text='print("fixed")'),
            self.call('run_command',argv=['{python}','main.py']),
            done,
        ])
        result=await self.app.run_user_task('Create program',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['verification']['status'],'passed')

    async def test_verified_copy_survives_later_failed_launch(self):
        pause={'role':'assistant','content':json.dumps({'summary':'unfinished','done':False,'state':'continue'})}
        self.app.request=AsyncMock(side_effect=[
            self.call('create_file',path='game.py',content='print("working")'),
            self.call('run_command',argv=['{python}','game.py'],launch=True),
            self.call('replace_text',path='game.py',old_text='print("working")',new_text='raise RuntimeError("broken")'),
            self.call('run_command',argv=['{python}','game.py'],launch=True),
            pause,
        ])
        result=await self.app.run_user_task('Create Pygame app',{},self.trace,self.parent)
        self.assertEqual(result['status'],'partial')
        state=self.app._state
        self.assertFalse(state['last_command']['passed'])
        copy=Path(state['verified_copy']['path'])
        self.assertEqual((copy/'game.py').read_text(),'print("working")')
        self.assertEqual((self.root/'game.py').read_text(),'raise RuntimeError("broken")')

    def test_resume_migrates_metadata_without_forcing_old_plan(self):
        self.app.load_or_start('Old goal')
        self.app._state.update(mode='old',plan=[dict(fixtures.task(),status='pending')],budget_spent=.3)
        self.app.save_state();self.app.load_or_start('C')
        self.assertEqual(self.app._state['mode'],'direct')
        self.assertEqual(self.app._state['budget_spent'],.3)
        self.assertEqual(self.app._state['plan'],[])
        self.assertEqual(self.app._state['legacy_plan'][0]['id'],'S1')
        self.assertTrue(list((self.app.logs_path/'autodev_history').glob('*before_direct*')))

    def test_history_compacts_only_at_limit_and_keeps_tool_pairs(self):
        self.app.options['writer_request_characters']=24000
        self.app.load_or_start('Create program');messages=self.app._state['messages']
        unchanged=copy.deepcopy(messages);self.app.fit_history(messages,self.app.available_tools());self.assertEqual(messages,unchanged)
        for i in range(15):
            messages.extend([{'role':'assistant','content':None,'tool_calls':[{'id':str(i),'type':'function','function':{'name':'read_file','arguments':'{"path":"main.py"}'}}]},
                             {'role':'tool','tool_call_id':str(i),'content':json.dumps({'ok':True,'result':{'output':'x'*2500}})}])
        self.app.fit_history(messages,self.app.available_tools())
        ids={c['id'] for m in messages for c in m.get('tool_calls',[])}
        self.assertEqual(ids,{m['tool_call_id'] for m in messages if m['role']=='tool'})
        self.assertLess(len(messages),32)

    async def test_read_only_loop_stops_after_bounded_recovery(self):
        observed=[]
        async def respond(app,role,*args,**kwargs):
            observed.append(role);cfg=app.role_settings(role);app.begin_usage(args[3],role,role,cfg['provider'],cfg['model'])
            if role=='strong':return {'role':'assistant','content':'Implement one pending feature using the source already inspected; do not rescan.'}
            return self.call('list_files',path='.')
        with patch('writer_transport.request',side_effect=respond),patch('responses_transport.request',side_effect=respond):
            result=await self.app.run_user_task('Build a program',{},self.trace,self.parent)
        self.assertEqual(result['status'],'partial')
        self.assertLessEqual(len(observed),10)
        self.assertIn('strong',observed)
        self.assertEqual(observed.count('strong'),1)
        self.assertIn('No progress',result['answer'])
