import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from orchestra import ModelResponseError, OutputLimitError
from responses_transport import input_items
from tests import test_single_model as fixtures

class Item:
    def __init__(self,**data): self.data=data
    def model_dump(self,**kwargs): return copy.deepcopy(self.data)

def response(output=(),status='completed',reason=None):
    return SimpleNamespace(id='resp_offline',model='gpt-6-luna',status=status,
        output=list(output),output_text='',incomplete_details=SimpleNamespace(reason=reason),
        usage=Item(input_tokens=100,output_tokens=80,total_tokens=180,
                   output_tokens_details={'reasoning_tokens':50},input_tokens_details={'cached_tokens':64}))

class Responses(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.SingleModel.setUp

    async def call_response(self,value):
        create=AsyncMock(return_value=value)
        self.app.clients['offline']=SimpleNamespace(responses=SimpleNamespace(create=create))
        usage={}
        result=await self.app.request('worker',[],self.app.available_tools(),'budget',usage,self.trace,self.task)
        return result,usage,create

    async def test_wire_format_usage_and_reasoning_round_trip(self):
        reasoning=Item(type='reasoning',id='rs_1',summary=[],encrypted_content='opaque')
        call=Item(type='function_call',id='fc_1',call_id='call_1',name='list_files',arguments='{}',status='completed')
        result,usage,create=await self.call_response(response([reasoning,call]))
        args=create.call_args.kwargs
        self.assertEqual(args['model'],'gpt-6-luna')
        self.assertEqual(args['reasoning'],{'effort':'low'})
        self.assertFalse(args['store'])
        self.assertNotIn('messages',args)
        self.assertNotIn('temperature',args)
        self.assertIn('name',args['tools'][0])
        self.assertEqual(usage['worker']['output_budget_tokens'],80)
        self.assertEqual(usage['worker']['reasoning_tokens'],50)
        wire=input_items([result,{'role':'tool','tool_call_id':'call_1','content':'OK'}])
        self.assertEqual(wire[0]['encrypted_content'],'opaque')
        self.assertEqual(wire[-1],{'type':'function_call_output','call_id':'call_1','output':'OK'})

    async def test_incomplete_only_preserves_complete_prefix(self):
        items=[Item(type='function_call',call_id='a',name='list_files',arguments='{}',status='completed'),
               Item(type='function_call',call_id='b',name='read_file',arguments='{',status='incomplete'),
               Item(type='function_call',call_id='c',name='list_files',arguments='{}',status='completed')]
        with self.assertRaises(OutputLimitError) as caught:
            await self.call_response(response(items,'incomplete','max_output_tokens'))
        self.assertEqual([c['id'] for c in caught.exception.tool_drafts],['a'])

    async def test_other_incomplete_reason_is_not_retried_as_length(self):
        with self.assertRaises(ModelResponseError) as caught:
            await self.call_response(response([], 'incomplete','content_filter'))
        self.assertNotIsInstance(caught.exception,OutputLimitError)

    def test_director_and_megaboss_are_blocked_even_if_configured(self):
        self.app.config['roles']['director']={'provider':'offline','model':'gpt-6.1-sol'}
        self.app.config['roles']['megaboss']={'provider':'offline','model':'gpt-6.1-astra'}
        for role in ['director','megaboss']:
            with self.assertRaises(ValueError): self.app.role_settings(role)
        self.app.config['roles']['worker']['model']='gpt-6.1-sol'
        with self.assertRaises(ValueError): self.app.role_settings('worker')

    async def test_completed_response_with_incomplete_call_is_rejected(self):
        item=Item(type='function_call',call_id='a',name='read_file',arguments='{',status='incomplete')
        with self.assertRaises(ModelResponseError):
            await self.call_response(response([item]))

    async def test_client_creation_with_optional_base_url(self):
        for url in (None, 'https://example.invalid/v1'):
            with self.subTest(base_url=url):
                self.app.clients.clear()
                self.app.config['providers']['offline']={'api_key':'offline-test-key'}
                if url:
                    self.app.config['providers']['offline']['base_url']=url
                client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response())))
                with patch('responses_transport.AsyncOpenAI',return_value=client) as constructor:
                    await self.app.request('worker',[],[],'budget',{},self.trace,self.task)
                self.assertEqual(constructor.call_args.kwargs['base_url'],url)
                self.assertEqual(constructor.call_args.kwargs['api_key'],'offline-test-key')
                client.responses.create.assert_awaited_once()

    def test_standard_gpt_pricing_with_cache_writes(self):
        from pathlib import Path
        self.app.pricing=json.loads((Path(__file__).resolve().parents[1]/'pricing.json').read_text())
        entry={'provider':'gpt','model':'gpt-6.1-sol','prompt_tokens':736,'cached_tokens':0,'completion_tokens':1500}
        self.assertAlmostEqual(self.app.estimated_cost(entry),0.016472)
        entry.update(prompt_tokens=1000,cached_tokens=200,cache_write_tokens=100,completion_tokens=100)
        self.assertAlmostEqual(self.app.estimated_cost(entry),0.00267)
