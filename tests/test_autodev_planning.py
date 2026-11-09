import json
import unittest
from unittest.mock import AsyncMock, patch
from tests import test_autodev as fixtures
from autodev_intent import continuation_intent


class PlanningTransitions(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.AutoDevelopment.setUp
    call=fixtures.AutoDevelopment.call
    set_plan=fixtures.AutoDevelopment.set_plan

    def test_natural_continuation_preserves_saved_plan_spend_and_position(self):
        self.set_plan();self.app._state['budget_spent']=.4;self.app._position=4;self.app.save_state()
        self.app.load_or_start('есть тз в what_to_do.md и уже часть написана. нужно продолжить')
        self.assertEqual(self.app.current_task()['id'],'S1')
        self.assertEqual(self.app._state['budget_spent'],.4)
        self.assertEqual(self.app._position,4)
        self.assertTrue(self.app._state['replan'])
        self.assertIn('нужно продолжить',self.app._state['clarifications'][-1])

    def test_continuation_clarification_is_retained_and_quotes_are_not_commands(self):
        intent,note=continuation_intent('есть ТЗ в what_to_do.md. нужно продолжить, добавь меню',True)
        self.assertTrue(intent);self.assertEqual(note,'есть ТЗ в what_to_do.md. нужно продолжить, добавь меню')
        for text in ['Новая задача: добавить кнопку продолжить','N: новая программа']:
            self.assertFalse(continuation_intent(text,True)[0])
        self.assertFalse(continuation_intent('нужно продолжить',False)[0])

    def test_new_task_archives_previous_checkpoint(self):
        self.set_plan();self.app.save_state()
        previous=self.app.state_path.read_text(encoding='utf-8')
        self.app.load_or_start('Новая задача: сделай калькулятор')
        archives=list((self.app.logs_path/'autodev_history').glob('*.json'))
        self.assertEqual(len(archives),1)
        self.assertEqual(archives[0].read_text(encoding='utf-8'),previous)
        self.assertEqual(self.app._state['plan'],[])

    async def test_read_loop_is_cut_off_then_implementation_is_available(self):
        requests=[]
        scripts=[self.call('list_files',path='.'),self.call('list_files',path='.'),
                 self.call('set_plan',tasks=[fixtures.task()]),
                 self.call('create_file',path='main.py',content='print("hello")'),
                 self.call('run_command',argv=['{python}','main.py'],purpose='verify',criteria=[1]),
                 self.call('finish_task',id='S1',summary='Runs')]
        async def request(role,messages,tools,*args,**kwargs):
            requests.append(({t['function']['name'] for t in tools},json.loads(messages[1]['content'])))
            return scripts[len(requests)-1]
        self.app.request=AsyncMock(side_effect=request)
        result=await self.app.run_user_task('Create a program',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(requests[2][0],{'set_plan'})
        self.assertEqual(requests[2][1]['phase']['name'],'planning')
        self.assertEqual(requests[2][1]['working_state']['phase'],'plan')
        self.assertIn('create_file',requests[3][0])
        self.assertEqual(requests[3][1]['phase']['name'],'implementation')

    async def test_second_plan_attempt_is_strong_and_does_not_resume_inspection(self):
        observed=[];offered=[]
        async def respond(app,role,messages,tools,*args,**kwargs):
            observed.append(role);offered.append({t['function']['name'] for t in tools})
            config=app.role_settings(role);app.begin_usage(args[1],role,role,config['provider'],config['model'])
            if len(observed)<=2:return self.call('list_files',path='.')
            if len(observed)==3:return self.call('read_file',path='main.py')
            return self.call('set_plan',tasks=[fixtures.task()])
        self.app.settings['execution']['max_steps_per_agent']=4
        with patch('writer_transport.request',side_effect=respond),patch('responses_transport.request',side_effect=respond):
            await self.app.run_user_task('Create a program',{},self.trace,self.parent)
        self.assertEqual(observed,['writer','writer','strong','strong'])
        self.assertEqual(offered[2:], [{'set_plan'},{'set_plan'}])
        self.assertTrue(self.app._state['plan'])

    async def test_refusal_to_plan_stops_with_specific_reason(self):
        self.app.request=AsyncMock(return_value=self.call('list_files',path='.'))
        result=await self.app.run_user_task('Build an app',{},self.trace,self.parent)
        self.assertEqual(self.app._state['stop_reason'],'planning_not_completed')
        self.assertEqual(self.app.request.await_count,4)
        self.assertNotIn('0/0',result['answer'])
        self.assertIn('разработка не начиналась',result['answer'])
