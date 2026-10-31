import unittest
from tests import test_single_model as fixtures


class OutputBudget(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SingleModel.setUp
    call = fixtures.SingleModel.call
    response = fixtures.SingleModel.response

    async def test_shared_budget_caps_requests_and_reserves_final(self):
        self.settings['generation']['max_completion_tokens']=2048
        self.settings['execution'].update(max_output_tokens_per_turn=3000,final_output_reserve_tokens=512)
        usage={}
        caps=[]
        async def request(role,messages,tools,status,turn_usage,*args,**kwargs):
            cap=kwargs['max_completion_tokens']
            caps.append((cap,bool(tools)))
            turn_usage.setdefault('iteration-1',{'output_budget_tokens':0})['output_budget_tokens']+=cap
            if tools:
                return self.response(self.call('list_files',path='.'))
            return {'role':'assistant','content':'Partial diagnosis'}
        self.app.request=request
        result=await self.app.run('director',[],{'calls':100},usage,self.trace,self.task)
        self.assertEqual(caps,[(2048,True),(440,True),(512,False)])
        self.assertEqual(sum(cap for cap,_ in caps),3000)

    async def test_previous_iteration_usage_counts_and_exhaustion_makes_no_request(self):
        self.settings['execution']['max_output_tokens_per_turn']=20000
        async def unexpected(*args,**kwargs):
            self.fail('Exhausted budget must not invoke the API')
        self.app.request=unexpected
        result=await self.app.run('director',[],{'calls':100},
            {'previous-iteration':{'output_budget_tokens':20000}},self.trace,self.task)
        self.assertEqual(result['stop_reason'],'output_budget')
        self.assertEqual(result['status'],'partial')
