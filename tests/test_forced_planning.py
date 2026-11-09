import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from tests import test_autodev as fixtures
from tests.test_responses_transport import response,Item

class ForcedPlanning(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.AutoDevelopment.setUp
    call=fixtures.AutoDevelopment.call

    def test_forced_plan_request_excludes_old_read_dialogue(self):
        self.app._state['planning_calls']=2
        self.app._state['task_history']=[{'task_id':'planning','messages':[self.call('read_file',path='main.py'),{'role':'tool','tool_call_id':'call','content':'{"ok":true}'}]}]
        messages=self.app.model_context('writer')
        self.assertEqual([m['role'] for m in messages],['system','user'])
        self.assertIn('PLANNING: inspection is finished',messages[0]['content'])

    async def test_forced_plan_routes_to_supported_strong_transport(self):
        self.app._state['planning_calls']=2
        create=AsyncMock(return_value=response())
        self.app.clients['gpt']=SimpleNamespace(responses=SimpleNamespace(create=create))
        self.app._position=2
        await self.app.request('worker',self.app.model_context('writer'),self.app.available_tools(),'budget',{},self.trace,self.parent)
        self.assertEqual(create.call_args.kwargs['tool_choice'],'required')
        self.assertEqual(self.app._position,0)
        self.assertNotIn('mimo',self.app.clients)

    async def test_responses_api_requires_the_only_plan_tool(self):
        from responses_transport import request
        self.app._state['planning_calls']=2
        create=AsyncMock(return_value=response())
        self.app.clients['gpt']=SimpleNamespace(responses=SimpleNamespace(create=create))
        await request(self.app,'strong',self.app.model_context('strong'),self.app.available_tools(),'budget',{},self.trace,self.parent)
        self.assertEqual(create.call_args.kwargs['tool_choice'],'required')
        self.assertEqual([t['name'] for t in create.call_args.kwargs['tools']],['set_plan'])

    async def test_refused_reads_keep_their_paths_without_failure_cascade(self):
        packets=[]
        async def respond(role,messages,tools,*args,**kwargs):
            packets.append(messages)
            self.app._state['planning_calls']=2
            return {'role':'assistant','content':None,'tool_calls':[
                self.call('read_file',path='main.py')['tool_calls'][0],
                dict(self.call('read_file',path='other.py')['tool_calls'][0],id='other')]}
        self.app.request=AsyncMock(side_effect=respond)
        self.app.settings['execution']['max_steps_per_agent']=1
        state=copy.deepcopy(self.app._state);state['planning_calls']=2
        def load(text):self.app._state=state
        self.app.load_or_start=load
        await self.app.run_user_task('C',{},self.trace,self.parent)
        outputs=[e['result'] for e in self.app._state['recent']]
        self.assertEqual([o['error'] for o in outputs],['planning_required','planning_required'])
        self.assertEqual([e['args']['path'] for e in self.app._state['recent']],['main.py','other.py'])
