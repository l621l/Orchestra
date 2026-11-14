import copy,json,unittest
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock,patch
from tests import test_autodev as fixtures
from direct_dev import DirectDevelopment
from orchestra import TurnTrace
from autodev_console import brief

class DirectLoop(unittest.IsolatedAsyncioTestCase):
    call=fixtures.AutoDevelopment.call

    async def test_sol_preserves_history_after_successful_commit(self):
        self.app.config['roles']['writer'] = {
            'provider': 'gpt', 'model': 'gpt-6.1-sol', 'transport': 'responses',
        }
        self.app.load_or_start('Implement project')
        messages = self.app._state['messages']
        for index, name in enumerate(('read_file', 'run_command', 'create_commit')):
            call_id = str(index)
            messages.extend([
                {'role': 'assistant', 'tool_calls': [{'id': call_id, 'type': 'function',
                    'function': {'name': name, 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': call_id, 'content': '{}'},
            ])
        with patch.object(self.app.repository, 'execute', return_value={'committed': True}):
            self.app.execute_tool('create_commit', {})
        with patch('responses_transport.request', new_callable=AsyncMock):
            await self.app.request_executor('writer', 'continuous_work', messages, [], '', {},
                                            self.trace, self.parent, False)
        self.assertEqual([item['tool_call_id'] for item in messages if item['role'] == 'tool'], ['0', '1', '2'])
        self.assertNotIn('compact_after_commit', self.app._state)
        self.assertFalse(any(event['event'] == 'history_compacted' for event in self.trace.events))

    def test_sol_bounds_new_reads_and_command_output_before_adding_them_to_history(self):
        self.app.config['roles']['writer'] = {
            'provider': 'gpt', 'model': 'gpt-6.1-sol', 'transport': 'responses',
        }
        source = {'ok': True, 'result': {
            'content': '\n'.join(f'{i}: ' + 'x' * 200 for i in range(1, 101)),
            'returned_start_line': 1, 'returned_end_line': 100,
        }}
        result = self.app.history_tool_result('read_file', {'path': 'main.py'}, source)['result']
        self.assertEqual(result['next_start_line'], result['returned_end_line'] + 1)
        self.assertLess(result['returned_end_line'], 100)
        self.assertEqual(source['result']['returned_end_line'], 100)

        command = {'ok': True, 'result': {'passed': False, 'output': 'x' * 5000 + 'FAILED'}}
        result = self.app.history_tool_result('run_command', {}, command)['result']
        self.assertFalse(result['passed'])
        self.assertTrue(result['output'].endswith('FAILED'))
        self.assertLess(len(result['output']), len(command['result']['output']))

    async def test_responses_writer_can_drop_old_reads_without_dropping_edits(self):
        self.app.config['roles']['writer']['transport'] = 'responses'
        (self.root / 'old.txt').write_text('old source', encoding='utf-8')
        self.app.request = AsyncMock(side_effect=[
            self.call('read_file', path='old.txt'),
            self.call('create_file', path='result.txt', content='new'),
            self.call('compact_context', paths=['old.txt', 'result.txt']),
            {'role': 'assistant', 'content': '{"summary":"Created","done":true,"state":"complete"}'},
        ])
        result = await self.app.run_user_task('Create result.txt', {}, self.trace, self.parent)
        messages = self.app._state['messages']
        calls = [call for item in messages for call in item.get('tool_calls', [])]
        self.assertEqual([call['function']['name'] for call in calls],
                         ['create_file', 'compact_context'])
        self.assertEqual([item['tool_call_id'] for item in messages if item['role'] == 'tool'],
                         [call['id'] for call in calls])
        self.assertNotIn('old source', json.dumps(messages))
        self.assertEqual(result['modified_files'], ['result.txt'])

    def test_responses_history_counts_encrypted_items_and_keeps_recent_pairs(self):
        from responses_transport import request_payload

        self.app.config['roles']['writer'] = {
            'provider': 'gpt', 'model': 'gpt-6.1-sol', 'transport': 'responses',
        }
        self.app.load_or_start('Develop program')
        messages = self.app._state['messages']
        self.app.options['writer_request_characters'] = 24000
        tools = self.app.available_tools()
        for index in range(3):
            call_id = f'call_{index}'
            messages.extend([
                {'role': 'assistant', '_response_items': [
                    {'type': 'reasoning', 'encrypted_content': str(index) * 6000},
                    {'type': 'function_call', 'call_id': call_id, 'name': 'read_file', 'arguments': '{}'},
                ]},
                {'role': 'tool', 'tool_call_id': call_id, 'content': '{}'},
            ])
        self.app.fit_history(messages, tools, 'Real instructions', max_output_tokens=4096)
        payload = request_payload(self.app, 'writer', messages, tools, 'Real instructions',
                                  max_output_tokens=4096)
        self.app.validate_request_input('writer', payload)
        self.assertEqual([item['encrypted_content'] for item in payload['input']
                          if item.get('type') == 'reasoning'], ['1' * 6000, '2' * 6000])
        for kind in ('function_call', 'function_call_output'):
            self.assertEqual([item['call_id'] for item in payload['input'] if item.get('type') == kind],
                             ['call_1', 'call_2'])

    @unittest.skipUnless(shutil.which('git'), 'Git is required')
    def test_direct_exposes_and_dispatches_git_for_a_repository(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        settings = copy.deepcopy(self.app.settings)
        settings['tools'].update(git_tools_enabled=True, git_history_enabled=True,
                                 allow_git_mutations=True)
        app = DirectDevelopment(self.app.config, self.root, settings, self.app.prompts,
                                self.app.pricing, logs_path=self.app.logs_path)
        app.load_or_start('Inspect repository')
        names = {tool['function']['name'] for tool in app.available_tools()}
        self.assertTrue({'git_status', 'git_diff', 'git_log', 'git_show',
                         'prepare_commit', 'create_commit'} <= names)
        self.assertNotIn('git_create_branch', names)
        self.assertIn('status', app.execute_tool('git_status', {}))

    async def test_debug_moves_from_diagnosis_to_edit_then_verification(self):
        self.app.settings['execution']['research_calls_before_action'] = 1
        path = self.root / 'main.py'
        path.write_text('print("old")\n', encoding='utf-8')
        self.app.request = AsyncMock(side_effect=[
            self.call('read_file', path='main.py'),
            self.call('run_command', argv=['{python}', 'main.py']),
            self.call('replace_text', path='main.py', old_text='old', new_text='new'),
            self.call('run_command', argv=['{python}', 'main.py']),
            {'role': 'assistant', 'content': 'Focused check passed.\n\n```json\n'
             '{"summary":"Fixed","done":true,"state":"complete"}\n```'},
        ])
        result = await self.app.run_user_task('D: Fix the wrong output', {}, self.trace, self.parent)
        requests = self.app.request.call_args_list
        for request in requests[1:3]:
            self.assertEqual({tool['function']['name'] for tool in request.args[2]},
                             {'create_file', 'replace_text'})
        self.assertIn('run_command', {tool['function']['name'] for tool in requests[3].args[2]})
        self.assertEqual(path.read_text(encoding='utf-8'), 'print("new")\n')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual(result['status'], 'completed')

    async def test_debug_can_create_missing_gitignore_at_edit_step(self):
        self.app.settings['execution']['research_calls_before_action'] = 1
        self.app.request = AsyncMock(side_effect=[
            self.call('list_files', path='.'),
            self.call('create_file', path='.gitignore', content='__pycache__/\n'),
            {'role': 'assistant', 'content': '{"summary":"Created ignore file","done":false,"state":"needs_user","blocker":{"action":"Review","reason":"Review requested","evidence":"File created"}}'},
        ])
        result = await self.app.run_user_task('D: Add missing .gitignore', {}, self.trace, self.parent)
        self.assertEqual((self.root / '.gitignore').read_text(), '__pycache__/\n')
        self.assertEqual(result['modified_files'], ['.gitignore'])

    async def test_debug_moves_to_focused_check_after_full_suite_failure(self):
        (self.root / 'main.py').write_text('value = "old"\n', encoding='utf-8')
        (self.root / 'suite.py').write_text('import main\nraise SystemExit(1)\n', encoding='utf-8')
        full = ['{python}', 'suite.py']
        focused = ['{python}', '-c', 'import main; assert main.value == "new"']
        self.app.request = AsyncMock(side_effect=[
            self.call('replace_text', path='main.py', old_text='old', new_text='new'),
            self.call('run_command', argv=full),
            self.call('run_command', argv=full),
            self.call('run_command', argv=focused),
            {'role': 'assistant', 'content': '{"summary":"Focused check passed; suite failed","done":true,"state":"complete"}'},
        ])
        result = await self.app.run_user_task('D: Fix the wrong value', {}, self.trace, self.parent)
        checks = [event for event in self.trace.events if event.get('tool') == 'run_command']
        self.assertFalse(checks[0]['result']['passed'])
        self.assertEqual(checks[1]['error_type'], 'debug_check_already_failed')
        self.assertTrue(checks[2]['result']['passed'])
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual(result['status'], 'completed')
    def setUp(self):
        import tempfile
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        base=Path(__file__).resolve().parents[1]
        settings=json.loads((base/'settings.json').read_text())
        config={'providers':{'mimo':{},'gpt':{}},'roles':{'writer':{'provider':'mimo','model':'mimo-v2.6-pro'},'strong':{'provider':'gpt','model':'gpt-6.1-sol'}}}
        with patch('orchestra.BASE',self.root):
            self.app=DirectDevelopment(config,self.root,settings,json.loads((base/'prompts.json').read_text()),json.loads((base/'pricing.json').read_text()))
        from unittest.mock import MagicMock
        self.app.dashboard=MagicMock();self.trace=TurnTrace(self.root/'logs','direct',self.root);self.parent=self.trace.start_task('worker','direct')

    def test_no_planning_gate_or_read_throttle(self):
        self.app.load_or_start('Build program')
        names={t['function']['name'] for t in self.app.available_tools()}
        self.assertFalse({'set_plan','finish_task','remember_work','read_spec','apply_patch','git_status'}&names)
        self.assertIn('replace_text',names)
        source=''.join(f'x{i}={i}\n' for i in range(245))
        self.app.execute_tool('create_file',{'path':'main.py','content':source})
        result=self.app.execute_tool('read_file',{'path':'main.py'})
        self.assertEqual(result['returned_end_line'],200)
        self.assertFalse(hasattr(self.app, 'requires_plan_call'))

    def test_direct_mode_works_without_alternation_settings(self):
        settings = copy.deepcopy(self.app.settings)
        for key in ('writer_calls', 'strong_calls', 'early_error_threshold', 'planning_attempt_limit'):
            settings['pipeline'].pop(key, None)
        engine = DirectDevelopment(self.app.config, self.root, settings, self.app.prompts,
                                   self.app.pricing, logs_path=self.app.logs_path)
        engine.load_or_start('Build program')
        self.assertEqual(engine.select_executor(), ('writer', 'continuous_work'))
        self.assertNotIn('planning_calls', engine._state)

    async def test_first_call_can_write_then_verify_without_protocol(self):
        done={'role':'assistant','content':json.dumps({'summary':'hello works','next_steps':'','done':True,'state':'complete'})}
        self.app.request=AsyncMock(side_effect=[self.call('create_file',path='main.py',content='print("hello")'),self.call('run_command',argv=['{python}','main.py']),done])
        result=await self.app.run_user_task('Create main.py printing hello',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['verification']['status'],'passed')
        calls=self.app.request.call_args_list
        self.assertIs(calls[0].args[1],calls[1].args[1])
        self.assertTrue(any(m['role']=='tool' for m in calls[1].args[1]))
        self.assertEqual((self.root/'main.py').read_text(),'print("hello")')
        self.assertIn('[DONE] Direct',brief(self.app))
        previous=self.app.request.await_count
        cached=await self.app.run_user_task('C',{},self.trace,self.parent)
        self.assertEqual(cached['status'],'completed')
        self.assertEqual(self.app.request.await_count,previous)

    async def test_failed_runtime_does_not_become_success(self):
        done={'role':'assistant','content':json.dumps({'summary':'done','done':True,'state':'complete'})}
        self.app.request=AsyncMock(side_effect=[self.call('create_file',path='main.py',content='raise RuntimeError("broken")'),self.call('run_command',argv=['{python}','main.py']),done,done,done])
        result=await self.app.run_user_task('Create program',{},self.trace,self.parent)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['verification']['status'],'failed')

    async def test_repairs_after_timeout_and_premature_report(self):
        done={'role':'assistant','content':json.dumps({'summary':'done','done':True,'state':'complete'})}
        self.app.request=AsyncMock(side_effect=[
            self.call('create_file',path='main.py',content='raise RuntimeError("broken")'),
            self.call('run_command',argv=['{python}','main.py']),
            TimeoutError(),
            done,
            self.call('replace_text',path='main.py',old_text='raise RuntimeError("broken")',new_text='print("fixed")'),
            self.call('run_command',argv=['{python}','main.py']),
            done,
        ])
        result=await self.app.run_user_task('Create program',{},self.trace,self.parent)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['verification']['status'],'passed')

    async def test_verified_copy_survives_later_failed_launch(self):
        pause={'role':'assistant','content':json.dumps({'summary':'unfinished','done':False,'state':'continue'})}
        self.app.request=AsyncMock(side_effect=[
            self.call('create_file',path='game.py',content='print("working")'),
            self.call('run_command',argv=['{python}','game.py'],launch=True),
            self.call('replace_text',path='game.py',old_text='print("working")',new_text='raise RuntimeError("broken")'),
            self.call('run_command',argv=['{python}','game.py'],launch=True),
            pause,
        ])
        result=await self.app.run_user_task('Create Pygame app',{},self.trace,self.parent)
        self.assertEqual(result['status'],'partial')
        state=self.app._state
        self.assertFalse(state['last_command']['passed'])
        copy=Path(state['verified_copy']['path'])
        self.assertEqual((copy/'game.py').read_text(),'print("working")')
        self.assertEqual((self.root/'game.py').read_text(),'raise RuntimeError("broken")')

    def test_resume_migrates_metadata_without_forcing_old_plan(self):
        self.app.load_or_start('Old goal')
        self.app._state.update(mode='old',plan=[dict(fixtures.task(),status='pending')],budget_spent=.3)
        self.app.save_state();self.app.load_or_start('C')
        self.assertEqual(self.app._state['mode'],'direct')
        self.assertEqual(self.app._state['budget_spent'],.3)
        self.assertEqual(self.app._state['plan'],[])
        self.assertEqual(self.app._state['legacy_plan'][0]['id'],'S1')
        self.assertTrue(list((self.app.logs_path/'autodev_history').glob('*before_direct*')))

    def test_history_compacts_only_at_limit_and_keeps_tool_pairs(self):
        self.app.options['writer_request_characters']=24000
        self.app.load_or_start('Create program');messages=self.app._state['messages']
        unchanged=copy.deepcopy(messages);self.app.fit_history(messages,self.app.available_tools());self.assertEqual(messages,unchanged)
        for i in range(15):
            messages.extend([{'role':'assistant','content':None,'tool_calls':[{'id':str(i),'type':'function','function':{'name':'read_file','arguments':'{"path":"main.py"}'}}]},
                             {'role':'tool','tool_call_id':str(i),'content':json.dumps({'ok':True,'result':{'output':'x'*2500}})}])
        self.app.fit_history(messages,self.app.available_tools())
        ids={c['id'] for m in messages for c in m.get('tool_calls',[])}
        self.assertEqual(ids,{m['tool_call_id'] for m in messages if m['role']=='tool'})
        self.assertLess(len(messages),32)

    async def test_read_only_loop_stops_after_bounded_recovery(self):
        observed=[]
        async def respond(app,role,*args,**kwargs):
            observed.append(role);cfg=app.role_settings(role);app.begin_usage(args[3],role,role,cfg['provider'],cfg['model'])
            if role=='strong':return {'role':'assistant','content':'Implement one pending feature using the source already inspected; do not rescan.'}
            return self.call('list_files',path='.')
        with patch('writer_transport.request',side_effect=respond),patch('responses_transport.request',side_effect=respond):
            result=await self.app.run_user_task('Build a program',{},self.trace,self.parent)
        self.assertEqual(result['status'],'partial')
        self.assertLessEqual(len(observed),10)
        self.assertIn('strong',observed)
        self.assertEqual(observed.count('strong'),1)
        self.assertIn('Изменений нет.', result['answer'])
        self.assertIn('без прогресса', result['stop_reason'])
