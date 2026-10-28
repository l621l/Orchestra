import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from openai import APIConnectionError
from types import SimpleNamespace

from execution_phase import ImplementationPhase
from orchestra import validate_settings, validate_prompts, TaskScheduler
from runtime_settings import load_runtime_settings
from tools import FILE_TOOLS, DELEGATE_TOOL
import test_base_tools


class RuntimeProfiles(unittest.TestCase):
    def test_default_and_project_are_independent(self):
        standard = load_runtime_settings('settings.json')
        project = load_runtime_settings('settings.json', 'project')
        validate_settings(standard)
        validate_settings(project)
        validate_prompts(json.loads(Path('prompts.json').read_text(encoding='utf-8')))
        self.assertEqual(standard['execution']['max_api_calls_per_turn'], 16)
        self.assertEqual(project['execution']['max_api_calls_per_turn'], 60)
        self.assertEqual(project['execution']['turn_timeout_seconds'], 1800)
        self.assertEqual(standard['git'], {'commit_mode': 'manual', 'allow_branch_creation': False})
        self.assertEqual(project['git'], {'commit_mode': 'autonomous', 'allow_branch_creation': True})
        self.assertFalse(standard['roles']['worker']['allow_editing'])
        self.assertTrue(project['roles']['worker']['allow_editing'])
        self.assertEqual(set(project['checks']), {'python_tests'})
        self.assertEqual(project['checks']['python_tests'][0], sys.executable)
        self.assertNotIn('profiles', project)
        self.assertEqual(load_runtime_settings('settings.json')['execution']['workflow'], 'focused')
        comparison = load_runtime_settings('settings.json', 'comparison')
        validate_settings(comparison)
        self.assertFalse(comparison['roles']['director']['allow_editing'])
        self.assertTrue(comparison['roles']['worker']['allow_editing'])
        self.assertEqual(comparison['execution']['development_strategy'], 'comparison')

    def test_unknown_profile_and_override_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            load_runtime_settings('settings.json', 'typo')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            settings = json.loads(Path('settings.json').read_text())
            settings['profiles']['project']['execution']['wrong_key'] = 1
            path.write_text(json.dumps(settings))
            with self.assertRaisesRegex(ValueError, 'wrong_key'):
                load_runtime_settings(path, 'project')

    def test_invalid_operational_limits_rejected(self):
        baseline = load_runtime_settings('settings.json')
        for section, field, bad in [('execution', 'workflow', 'bad'),
                                    ('execution', 'edit_retry_limit', 0),
                                    ('execution', 'max_implementation_recoveries', 0),
                                    ('execution', 'draft_context_characters', -1),
                                    ('execution', 'draft_context_characters', 20001),
                                    ('execution', 'verification_reserve_calls', 16),
                                    ('tool_limits', 'check_timeout_seconds', -1),
                                    ('tool_limits', 'max_parallel_local_tools', 1.5)]:
            settings = copy.deepcopy(baseline)
            settings[section][field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_settings(settings)
        baseline['roles']['worker']['allow_editing'] = 'yes'
        with self.assertRaisesRegex(ValueError, 'allow_editing'):
            validate_settings(baseline)

    def test_invalid_git_permissions_rejected(self):
        for policy in ([], {'commit_mode': []}, {'commit_mode': 'auto'},
                       {'allow_branch_creation': 'true'}, {'allow_push': True}):
            settings = load_runtime_settings('settings.json')
            settings['git'] = policy
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_settings(settings)

    def test_comparison_requires_coordinator_permissions_and_autonomous_git(self):
        for section, key, value in [('director', 'allow_editing', True),
                                    ('worker', 'allow_editing', False)]:
            settings = load_runtime_settings('settings.json', 'comparison')
            settings['roles'][section][key] = value
            with self.assertRaises(ValueError):
                validate_settings(settings)
        settings = load_runtime_settings('settings.json', 'comparison')
        settings['git']['commit_mode'] = 'manual'
        with self.assertRaises(ValueError):
            validate_settings(settings)

    def test_project_can_read_and_search_after_edits_and_research_limit(self):
        execution = load_runtime_settings('settings.json', 'project')['execution']
        phase = ImplementationPhase(execution, True)
        phase.enter('patch_applied')
        phase.research_calls = 100
        phase.update(20)
        self.assertFalse(phase.active)
        phase.update(execution['implementation_reserve_calls'])
        self.assertTrue(phase.active)
        for _ in range(20):
            phase.admit('read_file', {'path': 'whattodo.md'})
        phase.admit('search_text', {'query': 'R01'})
        names = {tool['function']['name'] for tool in phase.tools([*FILE_TOOLS, DELEGATE_TOOL])}
        self.assertIn('search_text', names)
        self.assertNotIn('delegate_task', names)
        with self.assertRaises(ValueError):
            phase.admit('delegate_task', {})

    def test_project_check_refuses_empty_suite_and_runs_real_tests(self):
        command = load_runtime_settings('settings.json', 'project')['checks']['python_tests']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'tests').mkdir()
            empty = subprocess.run(command, cwd=root, capture_output=True, timeout=10)
            self.assertNotEqual(empty.returncode, 0)
            (root / 'tests' / 'test_example.py').write_text(
                'import unittest\nclass Example(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(2 + 2, 4)\n')
            passed = subprocess.run(command, cwd=root, capture_output=True, timeout=10)
            self.assertEqual(passed.returncode, 0, passed.stderr.decode())


class ProjectWorkflow(unittest.IsolatedAsyncioTestCase):
    setUp = test_base_tools.RuntimeContracts.setUp
    call = test_base_tools.RuntimeContracts.call
    response = test_base_tools.RuntimeContracts.response
    run_responses = test_base_tools.RuntimeContracts.run_responses

    def truncated(self, content=None):
        return {'role': 'assistant', 'content': content, '_truncated': True, '_partial': bool(content)}

    def project(self):
        self.app.settings = load_runtime_settings('settings.json', 'project')

    async def test_project_length_recovery_keeps_reads_search_edits_and_disables_thinking(self):
        self.project()
        (self.root / 'spec.md').write_text('Return value 2.')
        result, messages = await self.run_responses([
            self.truncated(),
            self.response('read_file', {'path': 'spec.md'}),
            self.response('search_text', {'path': 'spec.md', 'query': 'value'}),
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
            self.response('run_check', {'name': 'unit'}),
            {'role': 'assistant', 'content': 'Implemented and checked.'}], mutation=True)
        requests = self.app.request.call_args_list
        self.assertTrue(requests[0].kwargs['thinking_enabled'])
        for request in requests[1:4]:
            self.assertFalse(request.kwargs['thinking_enabled'])
            self.assertFalse(request.args[8])
            names = {t['function']['name'] for t in request.args[2]}
            self.assertTrue({'read_file', 'search_text', 'create_file', 'replace_text'} <= names)
        self.assertTrue(requests[4].kwargs['thinking_enabled'])
        self.assertEqual(result['status'], 'completed')
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        self.assertTrue(all(o['ok'] for o in outputs))
        events = [e for e in self.trace.events if e['event'] == 'implementation_recovery_scheduled']
        self.assertEqual(events[0]['thinking'], 'disabled')
        self.assertTrue(any(e['event'] == 'implementation_recovery_progress' for e in self.trace.events))

    async def test_repeated_length_stops_after_one_retry_with_partial_report(self):
        self.project()
        result, _ = await self.run_responses([
            self.truncated(), self.truncated(),
            {'role': 'assistant', 'content': 'Incomplete: repeated output limits; no edits.'}], mutation=True)
        self.assertEqual(self.app.request.call_count, 3)
        self.assertTrue(self.app.request.call_args_list[-1].args[8])
        self.assertFalse(self.app.request.call_args_list[-1].kwargs['thinking_enabled'])
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(any(e['event'] == 'implementation_recovery_exhausted' for e in self.trace.events))

    async def test_recovery_applies_after_edits_and_resets_on_successful_edit(self):
        self.project()
        result, _ = await self.run_responses([
            self.truncated(),
            self.response('create_file', {'path': 'new.py', 'content': 'value = 1'}),
            self.truncated(),
            self.response('replace_text', {'path': 'new.py', 'old_text': '1', 'new_text': '2'}),
            self.response('run_check', {'name': 'unit'}),
            {'role': 'assistant', 'content': 'Implemented and checked.'}], mutation=True)
        requests = self.app.request.call_args_list
        self.assertFalse(requests[1].kwargs['thinking_enabled'])
        self.assertTrue(requests[2].kwargs['thinking_enabled'])
        self.assertFalse(requests[3].kwargs['thinking_enabled'])
        self.assertFalse(requests[3].args[8])
        self.assertEqual(result['status'], 'completed')
        recoveries = [e for e in self.trace.events if e['event'] == 'implementation_recovery_scheduled']
        self.assertEqual([e['attempt'] for e in recoveries], [1, 1])

    async def test_project_timeout_and_connection_recover_with_tools(self):
        for error in (TimeoutError(), APIConnectionError(request=SimpleNamespace())):
            with self.subTest(error=type(error).__name__):
                self.project()
                # Each subcase has the same model/tool contract but its own file state.
                (self.root / 'new.py').unlink(missing_ok=True)
                result, _ = await self.run_responses([
                    error,
                    self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
                    self.response('run_check', {'name': 'unit'}),
                    {'role': 'assistant', 'content': 'Recovered and checked.'}], mutation=True)
                self.assertFalse(self.app.request.call_args_list[1].kwargs['thinking_enabled'])
                self.assertFalse(self.app.request.call_args_list[1].args[8])
                self.assertEqual(result['status'], 'completed')

    async def test_repeated_prose_without_edits_is_bounded(self):
        self.project()
        result, _ = await self.run_responses([
            {'role': 'assistant', 'content': 'Here is a plan.'},
            {'role': 'assistant', 'content': 'Here is another plan.'},
            {'role': 'assistant', 'content': 'No implementation was completed.'}], mutation=True)
        self.assertEqual(self.app.request.call_count, 3)
        self.assertTrue(self.app.request.call_args_list[-1].args[8])
        self.assertEqual(result['status'], 'partial')

    async def test_failed_final_request_returns_runtime_report_and_preserves_edits(self):
        self.project()
        result, _ = await self.run_responses([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
            self.truncated('Useful partial findings.'), self.truncated(),
            APIConnectionError(request=SimpleNamespace())], mutation=True)
        self.assertEqual(self.app.request.call_count, 4)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['result_source'], 'runtime_fallback')
        self.assertIn('Workspace edits remain', result['answer'])
        self.assertIn('Useful partial findings.', result['answer'])
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 2')
        self.assertEqual(result['verification']['status'], 'passed')

    async def test_final_output_limit_preserves_partial_text(self):
        self.project()
        result, _ = await self.run_responses([
            self.truncated(), self.truncated(), self.truncated('Useful partial findings.')], mutation=True)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['result_source'], 'runtime_fallback')
        self.assertIn('Useful partial findings.', result['answer'])
        self.assertEqual(self.app.request.call_count, 3)

    async def test_recovery_retry_limit_is_configurable(self):
        self.project()
        self.app.settings['execution']['max_implementation_recoveries'] = 2
        result, _ = await self.run_responses([
            self.truncated(), self.truncated(), self.truncated(),
            {'role': 'assistant', 'content': 'Incomplete.'}], mutation=True)
        self.assertEqual(self.app.request.call_count, 4)
        self.assertEqual(result['status'], 'partial')
        recoveries = [e for e in self.trace.events if e['event'] == 'implementation_recovery_scheduled']
        self.assertEqual([e['attempt'] for e in recoveries], [1, 2])

    async def test_final_tool_call_cannot_restart_exhausted_recovery(self):
        self.project()
        result, _ = await self.run_responses([
            self.truncated(), self.truncated(),
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'})], mutation=True)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['stop_reason'], 'unexpected_final_tool_call')
        self.assertFalse((self.root / 'new.py').exists())
        self.assertEqual(self.app.request.call_count, 3)

    async def test_stream_discards_truncated_tool_arguments_and_sends_thinking_setting(self):
        from unittest.mock import AsyncMock
        self.project()
        self.app.role_settings.return_value = {'provider': 'mimo', 'model': 'offline-model'}
        part = SimpleNamespace(index=0, id='partial-call',
                               function=SimpleNamespace(name='create_file', arguments='{"path":"new.py","content":"'))
        chunk = SimpleNamespace(id='offline-response', model='offline-model', usage=None,
            choices=[SimpleNamespace(finish_reason='length', delta=SimpleNamespace(
                content=None, reasoning_content='Unverified design sketch.', tool_calls=[part]))])

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def __aiter__(self):
                async def chunks():
                    yield chunk
                return chunks()

        create = AsyncMock(return_value=Stream())
        self.app.clients = {'mimo': SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))}
        result = await self.app.request('director', [], [], 'offline', {}, self.trace, self.task,
                                        thinking_enabled=False, max_completion_tokens=8192)
        self.assertTrue(result['_truncated'])
        self.assertNotIn('tool_calls', result)
        self.assertEqual(create.call_args.kwargs['extra_body'], {'thinking': {'type': 'disabled'}})
        self.assertFalse((self.root / 'new.py').exists())
        request = next(e for e in self.trace.events if e['event'] == 'model_request_finished')
        self.assertEqual(request['response']['discarded_tool_calls'], 1)
        saved = next(e for e in self.trace.events if e['event'] == 'request_draft_saved')
        draft = json.loads((self.trace.directory / saved['path']).read_text(encoding='utf-8'))
        self.assertEqual(draft['reasoning'], 'Unverified design sketch.')
        self.assertEqual(draft['reason'], 'length')

    async def test_draft_archive_is_full_but_recovery_context_is_bounded(self):
        self.project()
        notes = 'START ' + 'x' * 10000 + ' END'
        self.trace.save_request_draft(self.task, 'req_offline', '', notes, 'length')
        stored = json.loads((self.trace.directory / 'drafts/req_offline.json').read_text())
        self.assertEqual(stored['reasoning'], notes)
        context = self.trace.draft_context(self.task, 200)
        self.assertIn('UNTRUSTED', context)
        self.assertIn('START', context)
        self.assertIn('END', context)
        self.assertNotIn('x' * 200, context)
        self.assertEqual(self.trace.draft_context(self.task, 0), '')
        self.assertEqual(self.trace.draft_context('another-task', 200), '')
        self.app.settings['execution']['draft_context_characters'] = 200
        _, messages = await self.run_responses([
            self.truncated(),
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
            self.response('run_check', {'name': 'unit'}),
            {'role': 'assistant', 'content': 'Implemented.'}], mutation=True)
        self.assertTrue(any(m.get('content') == context for m in messages))

    async def test_new_worker_receives_parent_draft_as_evidence_before_assignment(self):
        from unittest.mock import AsyncMock
        self.project()
        self.trace.save_request_draft(self.task, 'req_parent', '', 'Check invariant R20.', 'length')
        self.app.run = AsyncMock(return_value={'answer': 'Review complete.', 'status': 'completed', 'result_source': 'final'})
        scheduler = TaskScheduler(self.app, {'calls': 16}, {}, self.trace)
        submitted = scheduler.submit('worker-1', 'Review R20 in model.py.', self.task, 3, 500)
        await scheduler.tasks[submitted['task_id']]['asyncio_task']
        messages = self.app.run.call_args.args[1]
        self.assertIn('UNTRUSTED RECOVERY DRAFT', messages[-2]['content'])
        self.assertIn('Check invariant R20.', messages[-2]['content'])
        self.assertEqual(messages[-1]['content'], 'Review R20 in model.py.')
        self.assertTrue(any(e['event'] == 'request_draft_handoff' for e in self.trace.events))

    async def test_read_edit_check_loop_keeps_tools_in_project_profile(self):
        self.app.settings = load_runtime_settings('settings.json', 'project')
        result, messages = await self.run_responses([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
            *[self.response('read_file', {'path': 'new.py'}) for _ in range(10)],
            self.response('search_text', {'query': 'value', 'path': 'new.py'}),
            self.response('run_check', {'name': 'unit'}),
            {'role': 'assistant', 'content': 'Implemented and checked.'}], mutation=True)
        self.assertEqual(result['status'], 'completed')
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        self.assertTrue(all(item['ok'] for item in outputs))
        self.assertTrue(any('PROJECT FINALIZATION RESERVE' in (m.get('content') or '') for m in messages))
