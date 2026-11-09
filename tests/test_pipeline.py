import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from orchestra import TurnTrace
from pipeline import Pipeline

BASE=Path(__file__).resolve().parents[1]

class Alternating(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        settings=json.loads((BASE/'settings.json').read_text())
        config={'providers':{'mimo':{},'gpt':{}},'roles':{
            'writer':{'provider':'mimo','model':'mimo-v2.6-pro'},
            'strong':{'provider':'gpt','model':'gpt-6.1-sol'}}}
        with patch('orchestra.BASE',self.root):
            self.app=Pipeline(config,self.root,settings,json.loads((BASE/'prompts.json').read_text()),{})
        self.app.dashboard=MagicMock()
        self.trace=TurnTrace(self.root/'logs','offline',self.root)
        self.task=self.trace.start_task('worker','offline')

    def call(self,name,**args):
        return {'id':name,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}

    async def test_exact_sequence_and_shared_history(self):
        observed=[]; histories=[]
        async def respond(app,role,messages,*args,**kwargs):
            observed.append(role); histories.append(messages)
            return {'role':'assistant','content':'ok'}
        messages=[{'role':'user','content':'task'}]
        with patch('writer_transport.request',side_effect=respond), patch('responses_transport.request',side_effect=respond):
            for _ in range(12):
                await self.app.request('worker',messages,[],'budget',{},self.trace,self.task)
        self.assertEqual(observed,(['writer']*5+['strong'])*2)
        self.assertTrue(all(h is messages for i,h in enumerate(histories) if i % 6 != 5))
        self.assertTrue(all(h is not messages for i,h in enumerate(histories) if i % 6 == 5))

    async def test_early_strong_after_errors_and_reset(self):
        messages=[{'role':'tool','content':json.dumps({'ok':False})} for _ in range(3)]
        with patch('responses_transport.request',new_callable=AsyncMock) as strong:
            await self.app.request('worker',messages,[],'',{},self.trace,self.task)
        self.assertEqual(strong.call_args.args[1],'strong')
        self.assertEqual(self.app._position,0)

    async def test_edits_complete_without_running_configured_tests(self):
        responses=[{'role':'assistant','content':None,'tool_calls':[self.call('create_file',path='value.txt',content='correct')]},
            {'role':'assistant','content':json.dumps({'summary':'created','next_steps':'','done':True,'state':'complete','blocker':None})}]
        self.app.request=AsyncMock(side_effect=responses)
        self.app.repository.run_check=MagicMock(side_effect=AssertionError('must not auto-run'))
        result=await self.app.run_user_task('Create value.txt',{},self.trace,self.task)
        self.assertEqual(result['status'],'completed')
        self.assertEqual((self.root/'value.txt').read_text(),'correct')
        self.app.repository.run_check.assert_not_called()
        self.assertNotEqual(result['verification']['status'],'passed')

    async def test_continuation_preserves_position_and_original_scope(self):
        async def respond(app,role,messages,*args,**kwargs):
            return {'role':'assistant','content':json.dumps({'summary':'progress','next_steps':'more','done':False,'state':'continue'})}
        self.app.settings['execution']['max_api_calls_per_turn']=1
        with patch('writer_transport.request',side_effect=respond):
            first = await self.app.run_user_task('Original scope',{},self.trace,self.task)
        saved=json.loads(self.app.state_path.read_text())
        self.assertEqual(saved['position'],1,first)
        self.app._position=0
        with patch('writer_transport.request',side_effect=respond):
            await self.app.run_user_task('продолжи, add menu',{},self.trace,self.task)
        saved=json.loads(self.app.state_path.read_text())
        self.assertEqual(saved['position'],2)
        self.assertIn('Original scope',str(saved['messages']))
        self.assertIn('add menu',str(saved['messages']))

    async def test_failure_preserves_edit_and_does_not_retry_paid_call(self):
        responses=[{'role':'assistant','content':None,'tool_calls':[self.call('create_file',path='value.txt',content='saved')]},RuntimeError('API unavailable')]
        self.app.request=AsyncMock(side_effect=responses)
        result=await self.app.run_user_task('Create value.txt',{},self.trace,self.task)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['modified_files'],['value.txt'])
        self.assertEqual((self.root/'value.txt').read_text(),'saved')
        self.assertEqual(self.app.request.await_count,2)

    async def test_plain_answer_cannot_claim_completion(self):
        self.app.request=AsyncMock(return_value={'role':'assistant','content':'all done'})
        result=await self.app.run_user_task('Explain value',{},self.trace,self.task)
        self.assertEqual(result['status'],'partial')

    async def test_strong_chat_transport_configurable(self):
        self.app.config['roles']['strong']={'provider':'mimo','model':'custom-strong','transport':'chat'}
        self.app._position=5
        with patch('writer_transport.request',new_callable=AsyncMock) as chat:
            await self.app.request('worker',[],[],'',{},self.trace,self.task)
        self.assertEqual(chat.call_args.args[1],'strong')

    async def test_real_loop_switches_and_strong_model_edits(self):
        roles=[]
        async def respond(app,role,messages,*args,**kwargs):
            roles.append(role)
            if len(roles) <= 5:
                return {'role':'assistant','content':None,'tool_calls':[self.call('list_files',path='.')]}
            if len(roles) == 6:
                return {'role':'assistant','content':None,'tool_calls':[self.call('create_file',path='value.txt',content='strong edit')]}
            return {'role':'assistant','content':json.dumps({'summary':'created','next_steps':'','done':True,'state':'complete'})}
        with patch('writer_transport.request',side_effect=respond), patch('responses_transport.request',side_effect=respond):
            result=await self.app.run_user_task('Create value.txt',{},self.trace,self.task)
        self.assertEqual(roles,['writer']*5+['strong','writer'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual((self.root/'value.txt').read_text(),'strong edit')

    def test_large_history_compacts_without_reasoning_or_raw_logs(self):
        messages=[{'role':'user','content':'Original goal '+('a'*100000)},
                  {'role':'assistant','content':'old output '*20000,
                   '_response_items':[{'type':'reasoning','encrypted_content':'DO_NOT_SEND_REASONING'}]},
                  {'role':'tool','tool_call_id':'old','content':json.dumps({'ok':True,'result':{'output':'DO_NOT_SEND_OLD_LOG'*20000}})},
                  {'role':'user','content':'Latest requirement: menu'},
                  {'role':'assistant','content':json.dumps({'summary':'implemented movement','next_steps':'fix menu','state':'continue'})}]
        for i in range(15):
            call=self.call('replace_text',path='game.py',old_text='old '+str(i),new_text='new '+str(i))
            call['id']=str(i)
            messages.extend([{'role':'assistant','tool_calls':[call]},
                             {'role':'tool','tool_call_id':str(i),'content':json.dumps({'ok':True,'result':{'applied':True,'files':['game.py']}})}])
        original=json.dumps(messages)
        compact=self.app.strong_context(messages)
        packet=json.loads(compact[1]['content'])
        self.assertLessEqual(len(compact[1]['content']),6000)
        self.assertIn('Latest requirement: menu',str(packet))
        self.assertIn('fix menu',str(packet))
        self.assertIn('game.py',str(packet))
        self.assertIn('new 14',str(packet))
        self.assertNotIn('DO_NOT_SEND_',str(compact))
        self.assertFalse(any(m.get('tool_calls') or m.get('_response_items') or m['role']=='tool' for m in compact))
        self.assertEqual(json.dumps(messages),original)

    async def test_resource_status_map_is_not_forwarded_to_strong(self):
        self.app._position=5
        with patch('responses_transport.request',new_callable=AsyncMock) as strong:
            await self.app.request('worker',[{'role':'user','content':'Goal'}],[],
                'Remaining calls: 7\nHUGE_GENERATED_MAP'+('x'*100000),{},self.trace,self.task)
        self.assertNotIn('HUGE_GENERATED_MAP',strong.call_args.args[4])

    async def test_both_transports_reject_oversize_before_api_and_usage(self):
        from responses_transport import request as responses_request
        from writer_transport import request as chat_request
        self.app.options['strong_request_characters']=4000
        for transport,request in [('responses',responses_request),('chat',chat_request)]:
            self.app.config['roles']['strong']['transport']=transport
            usage={}
            with self.assertRaisesRegex(ValueError,'no API request sent'):
                await request(self.app,'strong',[{'role':'user','content':'x'*10000}],[],
                              'budget',usage,self.trace,self.task)
            self.assertEqual(usage,{})
            self.assertEqual(self.app.clients,{})

    async def test_actual_responses_payload_is_bounded_with_tool_schemas(self):
        from types import SimpleNamespace
        from tests.test_responses_transport import response
        create=AsyncMock(return_value=response())
        self.app.clients['gpt']=SimpleNamespace(responses=SimpleNamespace(create=create))
        self.app._position=5
        await self.app.request('worker',[{'role':'user','content':'goal '*100000}],
                               self.app.available_tools(),'budget\n'+'huge map'*100000,
                               {},self.trace,self.task)
        payload=create.call_args.kwargs
        self.assertLessEqual(len(json.dumps(payload,ensure_ascii=False)),16000)
        self.assertTrue(payload['tools'])
        self.assertNotIn('huge map',str(payload))
