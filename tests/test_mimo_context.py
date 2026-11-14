import copy,json,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from tests import test_direct_dev as fixtures
from tests.test_responses_transport import Item
from writer_transport import request,wire_messages

class MiMoContext(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.DirectLoop.setUp

    def response(self,reasoning='reasoning must be retained'):
        return SimpleNamespace(id='offline',model='mimo-v2.6-pro',usage=Item(prompt_tokens=50,completion_tokens=20,total_tokens=70),
            choices=[SimpleNamespace(finish_reason='tool_calls',message=Item(role='assistant',content=None,
                reasoning_content=reasoning,tool_calls=[{'id':'read1','type':'function','function':{'name':'read_file','arguments':'{"path":"main.py"}'}}]))])

    async def test_thinking_stays_enabled_on_second_working_call_and_reasoning_round_trips(self):
        self.app.load_or_start('Develop program')
        thought='long reasoning '*400
        self.trace.on_progress = lambda event: None
        create=AsyncMock(return_value=self.response(thought))
        self.app.clients['mimo']=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        messages=[{'role':'user','content':'Develop program'}];usage={}
        result=await request(self.app,'writer',messages,self.app.available_tools(),'budget',usage,self.trace,self.parent)
        messages.extend([result,{'role':'tool','tool_call_id':'read1','content':'{"ok":true,"result":{"content":"1: code"}}'}])
        await request(self.app,'writer',messages,self.app.available_tools(),'budget',usage,self.trace,self.parent)
        self.assertEqual([c.kwargs['extra_body']['thinking']['type'] for c in create.call_args_list],['enabled','enabled'])
        self.assertTrue(all(not call.kwargs.get('stream', False) for call in create.call_args_list))
        assistant=next(m for m in create.call_args.kwargs['messages'] if m['role']=='assistant')
        self.assertEqual(assistant['reasoning_content'],thought)
        self.assertNotIn('_reasoning_owner',assistant)

    def test_legacy_or_foreign_calls_are_factual_context_not_broken_thinking_history(self):
        config={'provider':'mimo','model':'mimo-v2.6-pro'}
        messages=[{'role':'assistant','content':None,'tool_calls':[{'id':'old','type':'function','function':{'name':'read_file','arguments':'{"path":"main.py"}'}}],
                   'reasoning_content':'FOREIGN_SECRET','_reasoning_owner':'other/model'},
                  {'role':'tool','tool_call_id':'old','content':'{"ok":true,"result":{"content":"1: class Game:"}}'}]
        original=copy.deepcopy(messages);wire=wire_messages(messages,config)
        self.assertEqual([m['role'] for m in wire],['user'])
        self.assertIn('class Game',wire[0]['content'])
        self.assertNotIn('FOREIGN_SECRET',str(wire))
        self.assertEqual(messages,original)

    def test_full_source_and_reasoning_survive_above_old_24k_cap(self):
        self.app.load_or_start('Develop program')
        messages=self.app._state['messages']
        messages.extend([{'role':'assistant','content':None,'reasoning_content':'R'*28000,'_reasoning_owner':'mimo/mimo-v2.6-pro',
                          'tool_calls':[{'id':'own','type':'function','function':{'name':'read_file','arguments':'{"path":"main.py"}'}}]},
                         {'role':'tool','tool_call_id':'own','content':json.dumps({'ok':True,'result':{'content':'full source '*1000}})}])
        before=copy.deepcopy(messages)
        self.app.fit_history(messages,self.app.available_tools())
        self.assertEqual(messages,before)
        self.assertGreater(len(json.dumps(messages)),24000)
        self.assertEqual(self.app.options['writer_request_characters'],192000)

    def test_actual_compaction_has_observations_and_never_cuts_retained_reasoning(self):
        self.app.load_or_start('Develop program');self.app.options['writer_request_characters']=24000
        self.app.execute_tool('create_file',{'path':'main.py','content':'class Game:\n    def step(self): return 1\n'+'\n'.join(f'value{i}={i}' for i in range(250))})
        result=self.app.execute_tool('read_file',{'path':'main.py'})
        messages=self.app._state['messages']
        for i in range(10):
            messages.extend([{'role':'assistant','content':None,'reasoning_content':'thought '*250,'_reasoning_owner':'mimo/mimo-v2.6-pro',
                'tool_calls':[{'id':str(i),'type':'function','function':{'name':'read_file','arguments':'{"path":"main.py"}'}}]},
                {'role':'tool','tool_call_id':str(i),'content':json.dumps({'ok':True,'result':result})}])
        self.app.fit_history(messages,self.app.available_tools())
        handoff=next(m['content'] for m in messages if m.get('_handoff'))
        facts=json.loads(handoff.split('\n',1)[1])
        self.assertTrue(facts['observed'])
        self.assertIn('main.py',str(facts))
        self.assertIn('class Game',str(facts))
        for m in messages:
            if m.get('_reasoning_owner'):self.assertEqual(m['reasoning_content'],'thought '*250)
        ids={c['id'] for m in messages for c in m.get('tool_calls',[])}
        self.assertEqual(ids,{m['tool_call_id'] for m in messages if m['role']=='tool'})

    def test_mimo_reasoning_is_not_passed_to_sol(self):
        self.app.load_or_start('Develop program')
        messages=[{'role':'user','content':'Develop program'}, {'role':'assistant','content':'Working','reasoning_content':'PRIVATE_REASONING','_reasoning_owner':'mimo/mimo-v2.6-pro'}]
        self.assertNotIn('PRIVATE_REASONING',str(self.app.strong_context(messages)))
