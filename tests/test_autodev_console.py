import json
import unittest
from tests import test_autodev as fixtures
from autodev_console import brief
from autodev_intent import input_command


class SimpleConsole(unittest.TestCase):
    setUp=fixtures.AutoDevelopment.setUp
    set_plan=fixtures.AutoDevelopment.set_plan

    def test_brief_has_status_progress_cost_and_commands_without_api(self):
        self.set_plan();self.app._state.update(status='partial',budget_spent=.0214)
        self.app.save_state()
        text=brief(self.app)
        self.assertIn('[PAUSE]',text)
        self.assertIn('0/1',text)
        self.assertIn(f'$0.0214/${self.app.options["max_task_cost_usd"]:.2f}',text)
        self.assertIn('C=continue',text)
        self.assertIn('N: <task>=new',text)
        self.assertEqual(self.app.clients,{})

    def test_c_continues_without_replanning_or_resetting_spend(self):
        self.set_plan();self.app._state.update(status='partial',budget_spent=.4)
        self.app.save_state();self.app.load_or_start('C')
        self.assertFalse(self.app._state['replan'])
        self.assertEqual(self.app._state['budget_spent'],.4)
        self.assertEqual(self.app.current_task()['id'],'S1')

    def test_ordinary_message_is_clarification_of_active_work(self):
        self.set_plan();self.app._state['budget_spent']=.4
        self.app.save_state();self.app.load_or_start('Добавь меню')
        self.assertEqual(self.app._state['budget_spent'],.4)
        self.assertEqual(self.app._state['clarifications'],['Добавь меню'])
        self.assertEqual(self.app.current_task()['id'],'S1')

    def test_n_starts_new_task_strips_command_and_archives_old(self):
        self.set_plan();self.app.save_state()
        self.app.load_or_start('N: Сделай калькулятор')
        self.assertEqual(self.app._state['specification'],'Сделай калькулятор')
        self.assertEqual(self.app._state['budget_spent'],0)
        self.assertTrue(list((self.app.logs_path/'autodev_history').glob('*.json')))

    def test_completed_work_does_not_capture_a_new_plain_request(self):
        self.set_plan();self.app._state['status']='completed';self.app.save_state()
        self.app.load_or_start('Сделай другую программу')
        self.assertEqual(self.app._state['specification'],'Сделай другую программу')
        self.assertEqual(self.app._state['clarifications'],[])

    def test_codes_accept_case_and_keyboard_layout(self):
        for text in ['c','C','с']:
            self.assertEqual(input_command(text),('continue',''))
        self.assertEqual(input_command('N'),('new',''))
        self.assertEqual(input_command('т: проект'),('new','проект'))
        self.assertEqual(input_command('S'),('status',''))
        self.assertEqual(input_command('Q'),('exit',''))

    def test_unreadable_state_is_reported_and_new_task_preserves_it(self):
        self.app.state_path.write_text('broken state',encoding='utf-8')
        self.assertIn('STATE_ERROR',brief(self.app))
        self.app.load_or_start('N: new project')
        old=next((self.app.logs_path/'autodev_history').glob('*.json'))
        self.assertEqual(old.read_text(encoding='utf-8'),'broken state')
