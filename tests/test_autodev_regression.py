import copy
import json
import unittest
from tests import test_autodev as fixtures
from autodev_context import fit_event
from tool_errors import ToolError
from unittest.mock import AsyncMock

class ReadRecoveryRegression(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.AutoDevelopment.setUp
    call=fixtures.AutoDevelopment.call
    set_plan=fixtures.AutoDevelopment.set_plan

    def test_reads_are_bounded_whole_lines_with_exact_continuation(self):
        self.set_plan()
        source=''.join(f'value_{i} = {i}\n' for i in range(1,246))
        self.app.execute('create_file',{'path':'main.py','content':source})
        result=self.app.execute('read_file',{'path':'main.py','start_line':1,'end_line':245})
        self.assertEqual(result['returned_end_line'],40)
        self.assertEqual(result['next_start_line'],41)
        event={'tool':'read_file','args':{'path':'main.py','start_line':1,'end_line':40},'result':{'ok':True,'result':result}}
        fitted=fit_event(event,1000)
        actual=fitted['result']['result'];lines=actual['content'].splitlines()
        self.assertTrue(lines)
        self.assertEqual(actual['returned_end_line'],len(lines))
        self.assertEqual(actual['next_start_line'],len(lines)+1)
        self.assertNotIn('[omitted',actual['content'])
        for n,line in enumerate(lines,1):self.assertEqual(line,f'{n}: value_{n} = {n}')
        self.assertEqual(fitted['args']['end_line'],40)
        self.assertLessEqual(len(json.dumps(fitted,ensure_ascii=False)),1000)

    async def test_recovery_survives_successful_observation(self):
        self.set_plan()
        self.app._state.update(stalled=6,recovery=0)
        self.app.save_state()
        self.app.settings['execution']['max_steps_per_agent']=2
        seen=[]
        async def request(role,messages,*args,**kwargs):
            seen.append(json.loads(messages[1]['content']))
            self.app._state['stalled']=6
            return self.call('list_files',path='.')
        self.app.request=AsyncMock(side_effect=request)
        # Continue resets counters intentionally; start recovery during first request instead.
        async def first(role,messages,*args,**kwargs):
            seen.append(json.loads(messages[1]['content']))
            self.app._state.update(stalled=6,recovery=1,recovery_instruction='CHANGE APPROACH NOW')
            return self.call('list_files',path='.')
        self.app.request=AsyncMock(side_effect=first)
        await self.app.run_user_task('продолжи',{},self.trace,self.parent)
        self.assertEqual(seen[1]['recovery']['instruction'],'CHANGE APPROACH NOW')
        self.assertTrue(seen[1]['recovery']['active'])

    def test_dependency_probe_cannot_complete_a_task_even_if_labelled_verify(self):
        self.set_plan()
        self.app.execute('create_file',{'path':'main.py','content':'print("hello")'})
        probe=self.app.execute('run_command',{'argv':['{python}','-c','import json;print(json.__name__)'],
                                              'purpose':'verify','criteria':[1]})
        self.assertTrue(probe['passed'])
        self.assertFalse(probe['qualifies_as_verification'])
        with self.assertRaises(ToolError):self.app.execute('finish_task',{'id':'S1','summary':'done'})
        real=self.app.execute('run_command',{'argv':['{python}','main.py'],'purpose':'verify','criteria':[1]})
        self.assertIn('main.py',real['executed_project_files'])
        self.app.execute('finish_task',{'id':'S1','summary':'Ran real entry point'})

    def test_each_acceptance_item_requires_evidence(self):
        task=fixtures.task();task['acceptance']=['prints hello','returns success']
        self.set_plan([task]);self.app.execute('create_file',{'path':'main.py','content':'print("hello")'})
        self.app.execute('run_command',{'argv':['{python}','main.py'],'purpose':'verify','criteria':[1]})
        with self.assertRaises(ToolError) as error:self.app.execute('finish_task',{'id':'S1','summary':'done'})
        self.assertEqual(error.exception.details['missing_criteria'],[2])
        self.app.execute('run_command',{'argv':['{python}','main.py'],'purpose':'verify','criteria':[2]})
        self.app.execute('finish_task',{'id':'S1','summary':'done'})

    async def test_log_replay_stops_rereading_and_reaches_real_verification(self):
        source=''.join(f'value_{i} = {i}\n' for i in range(1,246))+'print("hello")\n'
        calls=[self.call('set_plan',tasks=[fixtures.task()]),self.call('create_file',path='main.py',content=source),
            self.call('read_file',path='main.py',start_line=1,end_line=245),
            self.call('run_command',argv=['{python}','-c','import json;print(json.__name__)']),
            self.call('read_file',path='main.py',start_line=41,end_line=245),
            self.call('run_command',argv=['{python}','main.py'],purpose='verify',criteria=[1]),
            self.call('finish_task',id='S1',summary='Actual project executed')]
        packets=[];wire=[]
        async def request(role,messages,*args,**kwargs):
            packets.append(json.loads(messages[1]['content']));wire.append(copy.deepcopy(messages))
            return calls[len(packets)-1]
        self.app.request=AsyncMock(side_effect=request)
        result=await self.app.run_user_task('Build program',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        source_results=[json.loads(m['content'])['result'] for m in wire[3] if m['role']=='tool' and 'returned_start_line' in json.loads(m['content']).get('result',{})]
        self.assertEqual(source_results[-1]['returned_start_line'],1)
        self.assertEqual(source_results[-1]['returned_end_line'],40)
        self.assertIn('1: value_1 = 1',source_results[-1]['content'])
        self.assertEqual(packets[-1]['verified_criteria'],[1])
