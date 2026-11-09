import json,unittest
from unittest.mock import patch
from tests import test_direct_dev as fixtures

class ExpertRouting(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.DirectLoop.setUp
    call=fixtures.DirectLoop.call

    def usage(self,app,role,args):
        cfg=app.role_settings(role);app.begin_usage(args[3],role,role,cfg['provider'],cfg['model'])

    async def test_twelve_productive_calls_do_not_invoke_sol(self):
        roles=[]
        async def respond(app,role,*args,**kwargs):
            roles.append(role);self.usage(app,role,args)
            if len(roles)<=12:return self.call('create_file',path=f'f{len(roles)}.py',content='value=1')
            return {'role':'assistant','content':json.dumps({'summary':'created','next_steps':'','done':True,'state':'complete'})}
        with patch('writer_transport.request',side_effect=respond),patch('responses_transport.request',side_effect=AssertionError('No periodic Sol')):
            result=await self.app.run_user_task('Create files',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(roles,['writer']*13)

    async def test_specific_question_gets_one_advice_then_writer_implements(self):
        roles=[];expert_packets=[]
        async def respond(app,role,messages,tools,*args,**kwargs):
            roles.append(role)
            # Resource status is args[0], usage is args[1] after messages/tools.
            cfg=app.role_settings(role);app.begin_usage(args[1],role,role,cfg['provider'],cfg['model'])
            n=len(roles)
            if n==1:return self.call('create_file',path='main.py',content='print("old")')
            if n==2:return self.call('ask_expert',question='How should the old output change to new?',context='main.py contains print("old"). User requests new.')
            if role=='strong':
                self.assertEqual(tools,[])
                self.assertLessEqual(kwargs['max_output_tokens'],1536)
                expert_packets.append(json.loads(messages[1]['content']))
                return {'role':'assistant','content':'Replace print("old") with print("new"), then run the entry point.'}
            if n==4:return self.call('replace_text',path='main.py',old_text='old',new_text='new')
            if n==5:return self.call('run_command',argv=['{python}','main.py'])
            return {'role':'assistant','content':json.dumps({'summary':'Changed output and ran it','next_steps':'','done':True,'state':'complete'})}
        with patch('writer_transport.request',side_effect=respond),patch('responses_transport.request',side_effect=respond):
            result=await self.app.run_user_task('Create working program',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(roles,['writer','writer','strong','writer','writer','writer'])
        self.assertIn('old output',expert_packets[0]['question'])
        self.assertEqual((self.root/'main.py').read_text(),'print("new")')

    async def test_final_report_stays_with_writer_even_with_pending_expert(self):
        self.app.load_or_start('Create program');self.app._position=5
        self.app.execute_tool('ask_expert',{'question':'A decision?','context':'Some concrete evidence'})
        async def writer(app,role,*args,**kwargs):
            self.usage(app,role,args)
            return {'role':'assistant','content':'{"summary":"Saved actual changes","next_steps":"remaining","done":false,"state":"continue"}'}
        with patch('writer_transport.request',side_effect=writer),patch('responses_transport.request',side_effect=AssertionError('No cold Sol final')):
            await self.app.request('worker',self.app._state['messages'],[],'Final report',{},self.trace,self.parent,force_final=True)
        self.assertIn('expert_pending',self.app._state)
        self.assertEqual(self.app.select_executor(True),('writer','final_report'))

    async def test_same_question_and_revision_reuses_advice(self):
        self.app.load_or_start('Create program');question={'question':'Which exact fix?','context':'An observed failure'}
        self.app.execute_tool('ask_expert',question)
        async def respond(app,role,*args,**kwargs):
            self.usage(app,role,args);return {'role':'assistant','content':'A specific fix.'}
        with patch('responses_transport.request',side_effect=respond) as expert:
            await self.app.request('worker',self.app._state['messages'],self.app.available_tools(),'budget',{},self.trace,self.parent)
            cached=self.app.execute_tool('ask_expert',question)
        self.assertEqual(expert.call_count,1)
        self.assertEqual(cached['cached_advice'],'A specific fix.')
        self.assertNotIn('expert_pending',self.app._state)

    async def test_direct_keeps_tools_and_full_request_cap_after_old_output_limit(self):
        self.app.load_or_start('Create program');usage={}
        entry=self.app.begin_usage(usage,'writer','writer','mimo','mimo-v2.6-pro')
        entry['output_budget_tokens']=24000
        seen=[]
        async def respond(app,role,messages,tools,*args,**kwargs):
            seen.append((role,tools,kwargs['force_final'],kwargs['max_output_tokens']))
            self.usage(app,role,(messages,tools,*args))
            return {'role':'assistant','content':'{"summary":"Saved work","next_steps":"remaining","done":false,"state":"continue"}'}
        with patch('writer_transport.request',side_effect=respond),patch('responses_transport.request',side_effect=AssertionError('No expert final')):
            await self.app.run_user_task('Create program',usage,self.trace,self.parent)
        self.assertGreater(len(seen), 1)
        self.assertTrue(all(role == 'writer' and tools and not final and cap == 4096
                            for role, tools, final, cap in seen))
