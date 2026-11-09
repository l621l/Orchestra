import copy
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from tests import test_autodev as fixtures
from autodev_work import remember_result, make_turn


class WorkingMemory(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.AutoDevelopment.setUp
    call=fixtures.AutoDevelopment.call
    set_plan=fixtures.AutoDevelopment.set_plan

    def test_failed_command_and_missing_capability_survive_read_noise(self):
        self.set_plan();state=self.app._state;task=self.app.current_task()
        remember_result(state,task,'run_command',{'argv':['{python}','main.py','--demo']},
                        {'ok':False,'error':'check_timeout','message':'Command timed out'})
        remember_result(state,task,'search_text',{'path':'main.py','query':'demo'},
                        {'ok':True,'result':{'matches_returned':0}})
        for _ in range(20):
            remember_result(state,task,'read_file',{'path':'main.py'},
                            {'ok':True,'result':{'content':'unchanged source'}})
        packet=json.loads(self.app.context(6000)[1]['content'])
        work=packet['working_state']
        self.assertEqual(work['phase'],'repair')
        self.assertIn('demo',work['diagnosis'])
        self.assertIn('Implement',work['next_action'])
        remember_result(state,task,'read_file',{'path':'main.py'},
                        {'ok':False,'error':'repeated_action','message':'Repeated read'})
        self.assertIn('demo',state['working_state']['diagnosis'])

    def test_writer_gets_linked_history_strong_only_gets_bounded_summary(self):
        self.set_plan();task=self.app.current_task()
        message=self.call('read_file',path='main.py',start_line=201,end_line=240)
        message['tool_calls'][0]['id']='read-1'
        results=[{'role':'tool','tool_call_id':'read-1','content':json.dumps({'ok':True,'result':{'content':'201: def run(self):','returned_start_line':201,'returned_end_line':201}})}]
        self.app._state['task_history']=[make_turn(task['id'],message,results)]
        writer=self.app.model_context('writer');strong=self.app.model_context('strong')
        self.assertEqual([m['role'] for m in writer],['system','user','assistant','tool'])
        self.assertEqual(writer[2]['tool_calls'][0]['id'],writer[3]['tool_call_id'])
        self.assertEqual([m['role'] for m in strong],['system','user'])
        self.assertLessEqual(len(strong[1]['content']),6000)

    def test_large_turn_is_omitted_atomically_not_cut_mid_tool_call(self):
        self.set_plan();task=self.app.current_task()
        message=self.call('create_file',path='big.py',content='x'*20000)
        results=[{'role':'tool','tool_call_id':'call','content':'{"ok":true}'}]
        self.app._state['task_history']=[make_turn(task['id'],message,results)]
        wire=self.app.model_context('writer')
        self.assertFalse(any(m['role']=='tool' for m in wire))
        self.assertFalse(any(m.get('tool_calls') for m in wire))

    def test_work_and_history_survive_continue_but_not_task_switch(self):
        self.set_plan()
        self.app.execute('remember_work',{'diagnosis':'Missing startup option','next_action':'Modify entry point'})
        self.app.save_state();self.app.load_or_start('продолжи')
        self.assertEqual(self.app._state['working_state']['next_action'],'Modify entry point')
        self.app._state['plan'][0]['status']='done'
        self.app._state['plan'].append(dict(fixtures.task('S2','New task'),status='pending'))
        work=json.loads(self.app.context(6000)[1]['content'])['working_state']
        self.assertEqual(work['task_id'],'S2')
        self.assertEqual(work['diagnosis'],'')

    async def test_full_repair_loop_retains_failure_then_changes_real_source(self):
        scripts=[self.call('set_plan',tasks=[fixtures.task()]),
            self.call('create_file',path='main.py',content='import sys\nif "--demo" not in sys.argv: raise SystemExit(2)\nprint("hello")\n'),
            self.call('run_command',argv=['{python}','main.py','--smoke'],purpose='verify',criteria=[1]),
            self.call('search_text',path='main.py',query='smoke'),
            self.call('read_file',path='main.py',start_line=1,end_line=40),
            self.call('replace_text',path='main.py',old_text='"--demo"',new_text='"--smoke"'),
            self.call('run_command',argv=['{python}','main.py','--smoke'],purpose='verify',criteria=[1]),
            self.call('finish_task',id='S1',summary='Added supported entry-point option and ran it')]
        snapshots=[]
        async def request(role,messages,*args,**kwargs):
            snapshots.append(copy.deepcopy(messages))
            return scripts[len(snapshots)-1]
        self.app.request=AsyncMock(side_effect=request)
        result=await self.app.run_user_task('Fix bounded startup.',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        packet=json.loads(snapshots[5][1]['content'])
        self.assertEqual(packet['working_state']['phase'],'repair')
        self.assertIn('smoke',packet['working_state']['diagnosis'])
        self.assertIn('Implement',packet['working_state']['next_action'])
        self.assertTrue(any(m['role']=='tool' for m in snapshots[5]))
        self.assertIn('--smoke',(self.root/'main.py').read_text())
