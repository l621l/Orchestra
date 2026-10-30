import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from session_state import SessionState
from tests import test_single_model as fixtures


class Continuation(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.SingleModel.setUp

    def outcome(self):
        return {'answer':json.dumps({'summary':'Implemented part; test X failed',
            'next_steps':'Fix test X, then finish CLI','done':False,'state':'continue','blocker':None}),
            'status':'partial','modified_files':['saved.py'],
            'verification':{'status':'failed','checks':{}}}

    async def test_one_session_saves_and_resume_includes_original_task_and_handoff(self):
        self.settings['execution'].update(iterations_per_task=1,stop_after_confirmation=False)
        self.app.run=AsyncMock(return_value=self.outcome())
        first=await self.app.run_user_task('Implement specification.md',{},self.trace,self.task)
        self.assertEqual(first['iterations'],1)
        self.assertEqual(self.app.run.call_count,1)
        self.app.run.reset_mock()
        await self.app.run_user_task('продолжи',{},self.trace,self.task)
        messages=self.app.run.call_args.args[1]
        self.assertEqual(messages[1]['content'],'Implement specification.md')
        self.assertIn('test X failed',messages[2]['content'])
        self.assertIn('Fix test X',messages[2]['content'])
        self.assertEqual(self.app.run.call_count,1)
        self.assertLess(len(messages),6)

    async def test_missing_state_never_calls_model(self):
        self.app.run=AsyncMock()
        result=await self.app.run_user_task('continue',{},self.trace,self.task)
        self.assertEqual(result['stop_reason'],'continuation_unavailable')
        self.app.run.assert_not_called()

    async def test_new_task_does_not_inherit_previous_task(self):
        self.settings['execution'].update(iterations_per_task=1,stop_after_confirmation=False)
        self.app.session_state.save('Old task','old handoff','old')
        self.app.run=AsyncMock(return_value=self.outcome())
        await self.app.run_user_task('New task',{},self.trace,self.task)
        messages=self.app.run.call_args.args[1]
        self.assertEqual(messages[1]['content'],'New task')
        self.assertNotIn('old handoff',json.dumps(messages))
        self.assertEqual(self.app.session_state.load()['original_task'],'New task')

    def test_restart_persistence_and_workspace_isolation(self):
        self.app.session_state.save('task','summary','turn')
        restarted=SessionState(self.app.logs_path,self.root)
        self.assertEqual(restarted.load()['handoff'],'summary')
        other=self.root/'other';other.mkdir()
        self.assertIsNone(SessionState(self.app.logs_path,other).load())

    async def test_corrupt_state_does_not_start_paid_work(self):
        self.app.session_state.path.parent.mkdir(parents=True)
        self.app.session_state.path.write_text('{broken')
        self.app.run=AsyncMock()
        result=await self.app.run_user_task('resume',{},self.trace,self.task)
        self.assertEqual(result['status'],'partial')
        self.app.run.assert_not_called()

    def test_production_defaults_are_one_session(self):
        settings=json.loads((fixtures.BASE/'settings.json').read_text())
        self.assertEqual(settings['execution']['iterations_per_task'],1)
        self.assertFalse(settings['execution']['stop_after_confirmation'])
        self.assertEqual(settings['execution']['max_api_calls_per_turn'],100)

    async def test_resume_with_clarification_preserves_original_and_current_focus(self):
        self.settings['execution'].update(iterations_per_task=1,stop_after_confirmation=False)
        self.app.session_state.save('Implement what_to_do.md','previous progress','old',
                                    clarifications=['Keep existing save files'])
        self.app.run=AsyncMock(return_value=self.outcome())
        await self.app.run_user_task('продолжи. Сделай красивое меню',{},self.trace,self.task)
        messages=self.app.run.call_args.args[1]
        self.assertEqual(messages[1]['content'],'Implement what_to_do.md')
        user_messages=[m['content'] for m in messages if m['role']=='user']
        self.assertIn('Keep existing save files',user_messages[-1])
        self.assertIn('Сделай красивое меню',user_messages[-1])
        self.assertIn('previous progress',user_messages[-2])
        saved=self.app.session_state.load()
        self.assertEqual(saved['original_task'],'Implement what_to_do.md')
        self.assertEqual(saved['clarifications'],['Keep existing save files','Сделай красивое меню'])
        self.app.run.reset_mock()
        await self.app.run_user_task('продолжи',{},self.trace,self.task)
        self.assertIn('Сделай красивое меню',json.dumps(self.app.run.call_args.args[1],ensure_ascii=False))

    def test_continuation_prefix_uses_word_boundary(self):
        for text in ['продолжи, меню','continue: menu','resume menu','дальше — меню']:
            self.assertTrue(SessionState.is_continue(text))
        self.assertFalse(SessionState.is_continue('continuation explanation'))
        self.assertFalse(SessionState.is_continue('продолжительность игры'))
        self.assertEqual(SessionState.parse_continue('продолжи. меню'),(True,'меню'))

    def test_layout_suggestion_does_not_rewrite_normal_english(self):
        text='ghjljk;b b dct nfrb cltkfq yjhvfkmyjt vty. rhfcbdjt'
        suggestion=SessionState.layout_suggestion(text)
        self.assertIn('продолжи',suggestion)
        self.assertIn('сделай нормальное меню красивое',suggestion)
        self.assertIsNone(SessionState.layout_suggestion('continue and improve the menu'))
        self.assertIsNone(SessionState.layout_suggestion('продолжи и исправь меню'))
