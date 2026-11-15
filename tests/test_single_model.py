import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from orchestra import Orchestra, TurnTrace, validate_settings
from tools import ProjectTools
from tool_contracts import validate_arguments
from tool_errors import ToolError
from git_tools import GIT_TOOLS

BASE = Path(__file__).resolve().parents[1]


class SingleModel(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = json.loads((BASE/'settings.json').read_text())
        self.settings['execution'].update(iterations_per_task=5,stop_after_confirmation=True)
        self.settings['tools']['git_tools_enabled'] = True  # Legacy Git opt-in tests.
        self.settings['checks'] = {'unit': ['{python}', '-B', '-c',
            "from pathlib import Path; assert Path('value.txt').read_text() == 'correct'"]}
        config = {'providers': {'offline': {}},
                  'roles': {'writer': {'provider': 'offline', 'model': 'mimo-v2.6-pro'}}}
        with patch('orchestra.BASE', self.root):
            self.app = Orchestra(config, self.root, self.settings,
                json.loads((BASE/'prompts.json').read_text()), {})
        self.app.dashboard = MagicMock()
        self.trace = TurnTrace(self.root/'logs', 'offline test', self.root)
        self.task = self.trace.start_task('worker', 'offline test')

    def call(self, tool_name, **args):
        return {'id': tool_name, 'type': 'function', 'function': {'name': tool_name, 'arguments': json.dumps(args)}}

    def response(self, *calls):
        return {'role': 'assistant', 'content': None, 'tool_calls': list(calls)}

    async def run_script(self, responses, budget=12):
        self.app.request = AsyncMock(side_effect=responses)
        messages = []
        pool = {'calls': budget}
        result = await self.app.run('worker', messages, pool, {}, self.trace, self.task)
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        return result, outputs, pool

    async def test_read_edit_check_and_one_model_configuration(self):
        (self.root/'value.txt').write_text('wrong')
        result, outputs, pool = await self.run_script([
            self.response(self.call('read_file', path='value.txt')),
            self.response(self.call('replace_text', path='value.txt', old_text='wrong', new_text='correct')),
            self.response(self.call('run_check', name='unit')),
            {'role':'assistant','content':'Done'}])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual(result['modified_files'], ['value.txt'])
        self.assertEqual(pool['calls'], 8)
        self.assertTrue(all(o['ok'] for o in outputs))

    async def test_sequential_edits_and_read_see_current_content(self):
        result, outputs, _ = await self.run_script([
            self.response(self.call('create_file', path='value.txt', content='wrong'),
                          self.call('replace_text', path='value.txt', old_text='wrong', new_text='correct'),
                          self.call('read_file', path='value.txt')),
            self.response(self.call('run_check', name='unit')),
            {'role':'assistant','content':'Done'}])
        self.assertEqual(result['status'], 'completed')
        self.assertIn('correct', outputs[2]['result']['content'])

    async def test_edit_then_check_batch_verifies_new_revision(self):
        result, outputs, _ = await self.run_script([
            self.response(self.call('create_file',path='value.txt',content='correct'),
                          self.call('run_check',name='unit')),
            {'role':'assistant','content':'Done'}])
        self.assertTrue(all(o['ok'] for o in outputs))
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['verification']['status'],'passed')

    async def test_check_before_edit_is_stale(self):
        (self.root/'value.txt').write_text('correct')
        result, _, _ = await self.run_script([
            self.response(self.call('run_check',name='unit'),
                          self.call('replace_text',path='value.txt',old_text='correct',new_text='wrong')),
            {'role':'assistant','content':'Done'}])
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['verification']['status'],'not_run_or_stale')

    async def test_precise_error_and_repair(self):
        (self.root/'value.txt').write_text('wrong')
        result, outputs, _ = await self.run_script([
            self.response(self.call('replace_text', path='value.txt', old_text='missing', new_text='correct')),
            self.response(self.call('replace_text', path='value.txt', old_text='wrong', new_text='correct')),
            self.response(self.call('run_check', name='unit')),
            {'role':'assistant','content':'Done'}])
        self.assertEqual(outputs[0]['error'], 'text_not_found')
        self.assertIn('focused read', outputs[0]['message'])
        self.assertEqual(result['status'], 'completed')

    async def test_failed_or_stale_verification_is_partial(self):
        for value in ['wrong', 'correct']:
            with self.subTest(value=value):
                (self.root/'value.txt').write_text(value)
                script = [self.response(self.call('run_check', name='unit'))]
                if value == 'correct':
                    script.append(self.response(self.call('replace_text', path='value.txt',
                                                         old_text='correct', new_text='wrong')))
                script.append({'role':'assistant','content':'Done'})
                result, _, _ = await self.run_script(script)
                self.assertEqual(result['status'], 'partial')
                self.assertNotEqual(result['verification']['status'], 'passed')

    async def test_no_checks_cannot_claim_verified_edit(self):
        self.app.repository.checks = {}
        result, _, _ = await self.run_script([
            self.response(self.call('create_file', path='value.txt', content='correct')),
            {'role':'assistant','content':'Done'}])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['verification']['status'], 'not_configured')
        self.assertNotIn('run_check', {t['function']['name'] for t in self.app.available_tools()})

    async def test_read_only_mode_and_hallucinated_tools_cannot_write(self):
        self.settings['tools']['editing_enabled'] = False
        result, outputs, _ = await self.run_script([
            self.response(self.call('create_file', path='value.txt', content='correct'),
                          self.call('delegate_task', task='anything')),
            {'role':'assistant','content':'Unable'}])
        self.assertTrue(all(o['error']=='tool_unavailable' for o in outputs))
        self.assertFalse((self.root/'value.txt').exists())

    async def test_final_call_never_executes_tools(self):
        result, outputs, pool = await self.run_script([
            self.response(self.call('create_file', path='value.txt', content='correct'))], budget=1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(pool['calls'], 0)
        self.assertEqual(self.app.request.call_args.args[2], [])
        self.assertFalse((self.root/'value.txt').exists())

    async def test_invalid_search_arguments_are_actionable(self):
        _, outputs, _ = await self.run_script([
            self.response(self.call('search_text', query='x', context_lines=30)),
            {'role':'assistant','content':'Done'}])
        self.assertEqual(outputs[0]['error'], 'invalid_arguments')
        self.assertIn('at most 5', outputs[0]['message'])

    def test_git_mutations_opt_in_and_non_git_workspaces(self):
        names = {t['function']['name'] for t in self.app.available_tools()}
        self.assertIn('create_file', names)
        self.assertNotIn('git_log', names)
        self.app.repository.available = True
        names = {t['function']['name'] for t in self.app.available_tools()}
        self.assertIn('git_status', names)
        self.assertNotIn('create_commit', names)
        self.settings['tools']['allow_git_mutations'] = True
        names = {t['function']['name'] for t in self.app.available_tools()}
        self.assertIn('create_commit', names)

    async def test_enabled_commit_requires_current_checks(self):
        self.app.repository.available = True
        self.settings['tools']['allow_git_mutations'] = True
        _, outputs, _ = await self.run_script([
            self.response(self.call('create_commit', message='test', paths=['value.txt'], fingerprint='a'*64)),
            {'role':'assistant','content':'Unable'}])
        self.assertEqual(outputs[0]['error'], 'verification_required')

    def test_settings_reject_shell_strings_and_delegation(self):
        for update in [{'checks': {'test': 'python -m unittest'}},
                       {'tools': {'editing_enabled':'yes'}}]:
            value = copy.deepcopy(self.settings)
            value.update(update)
            with self.assertRaises(ValueError):
                validate_settings(value)


class Contracts(unittest.TestCase):
    def test_empty_commit_paths_rejected_by_schema(self):
        tool = next(t for t in GIT_TOOLS if t['function']['name']=='create_commit')
        with self.assertRaises(ToolError):
            validate_arguments(tool, {'message':'test','paths':[],'fingerprint':'a'*64})

    def test_read_search_boundaries_and_secret_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'a.txt').write_text('needle\n'*250)
            (root/'config.local.json').write_text('needle')
            tools = ProjectTools(root)
            data = tools.execute('read_file', {'path':'a.txt'})
            self.assertTrue(data['truncated'])
            hits = tools.execute('search_text', {'query':'needle','max_results':2})
            self.assertEqual(hits['matches_returned'], 2)
            self.assertTrue(hits['next_cursor'])
            for path in ['../outside.txt', 'config.local.json']:
                with self.assertRaises(ToolError):
                    tools.execute('read_file', {'path':path})
