"""Offline acceptance tests: real dispatch, temporary workspaces and trace files."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from execution_phase import BLOCKER_TOOL
from git_tools import GIT_TOOLS, GIT_TOOL_NAMES, RepositoryTools
from orchestra import TurnTrace
from tool_contracts import validate_arguments
from tool_errors import ToolError
from tools import FILE_TOOLS, ProjectTools, parse_arguments
import test_reliability


class BaseToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='orchestra-base-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = ProjectTools(self.root)
        self.repo = RepositoryTools(self.root)

    def test_new_code_workflow_without_git(self):
        self.assertFalse(self.repo.available)
        created = self.repo.execute('create_file', {'path':'src/adder.py', 'content':'def add(a, b):\n    return a - b\n'})
        self.assertTrue(created['created'])
        self.assertIn('+def add', created['diff'])
        self.assertIn('adder.py', self.files.execute('list_files', {'path':'.'})['tree'])
        read = self.files.execute('read_file', {'path':'src/adder.py'})
        self.assertIn('return a - b', read['content'])
        self.assertEqual(self.files.execute('search_text', {'query':'return', 'path':'src'})['matches_returned'], 1)
        self.assertEqual(self.files.execute('search_regex', {'pattern':r'def \w+', 'path':'src'})['matches_returned'], 1)
        replaced = self.repo.execute('replace_text', {'path':'src/adder.py', 'old_text':'return a - b', 'new_text':'return a + b'})
        self.assertTrue(replaced['applied'])
        self.assertIn('-    return a - b', replaced['diff'])
        self.assertIn('+    return a + b', replaced['diff'])
        self.repo.checks = {'unit':[sys.executable, '-B', '-c', 'from src.adder import add; assert add(2,3)==5']}
        checked = self.repo.execute('run_check', {'name':'unit'})
        self.assertTrue(checked['passed'], checked)
        self.assertFalse((self.root/'.git').exists())

    def test_create_never_overwrites_and_allows_empty_file(self):
        args={'path':'empty.txt','content':''}
        self.repo.execute('create_file', args)
        with self.assertRaises(ToolError) as error:
            self.repo.execute('create_file', {**args, 'content':'overwrite'})
        self.assertEqual(error.exception.code, 'file_exists')
        self.assertEqual((self.root/'empty.txt').read_bytes(), b'')

    def test_git_patch_transport_preserves_lf_bytes_on_windows(self):
        # write_bytes is deliberate: write_text would create CRLF on Windows
        # and hide the bug reproduced with the user's LF README.
        self.repo._run(['init', '-q'])
        self.repo._run(['config', 'core.autocrlf', 'false'])
        original = '# Example\nold description\n'
        updated = '# Example\nNew description: проверено\n'
        (self.root / 'README.md').write_bytes(original.encode('utf-8'))
        patch_text = ('diff --git a/README.md b/README.md\n'
                      '--- a/README.md\n+++ b/README.md\n@@ -1,2 +1,2 @@\n'
                      ' # Example\n-old description\n+New description: проверено\n')
        self.repo.execute('apply_patch', {'patch': patch_text})
        self.assertEqual((self.root / 'README.md').read_bytes(), updated.encode('utf-8'))

    def test_git_output_does_not_normalize_crlf_patch_content(self):
        self.repo._run(['init', '-q'])
        self.repo._run(['config', 'core.autocrlf', 'false'])
        (self.root / 'a.txt').write_bytes(b'old\r\n')
        self.repo._run(['add', 'a.txt'])
        result = self.repo._run(['show', ':a.txt'])
        self.assertEqual(result['stdout'], 'old\r\n')

    def test_concurrent_creation_preserves_other_writer(self):
        real_link=os.link
        def competing_link(source, target):
            Path(target).write_bytes(b'other writer')
            return real_link(source,target)
        with patch('git_tools.os.link', side_effect=competing_link):
            with self.assertRaises(ToolError) as error:
                self.repo.execute('create_file', {'path':'race.txt','content':'ours'})
        self.assertEqual(error.exception.code, 'file_exists')
        self.assertEqual((self.root/'race.txt').read_bytes(), b'other writer')
        self.assertEqual([p.name for p in self.root.iterdir()], ['race.txt'])

    def test_create_io_failure_leaves_no_file(self):
        with patch('git_tools.os.link', side_effect=OSError('unavailable')):
            with self.assertRaises(ToolError) as error:
                self.repo.execute('create_file', {'path':'new.txt','content':'ours'})
        self.assertEqual(error.exception.code, 'write_failed')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_diff_failure_after_edit_still_returns_success(self):
        with patch.object(self.repo, '_edit_diff', side_effect=OSError('simulated')):
            result=self.repo.execute('create_file',{'path':'a.txt','content':'old'})
            self.assertTrue(result['applied'])
            self.assertTrue(result['warnings'])
            result=self.repo.execute('replace_text',{'path':'a.txt','old_text':'old','new_text':'new'})
            self.assertTrue(result['applied'])
            self.assertTrue(result['warnings'])
        self.assertEqual((self.root/'a.txt').read_text(),'new')

    def test_forbidden_paths_and_binary_content(self):
        for name in ['../outside.txt', 'config.local.json', '.git/config',
                     'file.txt:stream', 'NUL.txt', 'file.txt.', 'file.txt ', 'C:relative.txt']:
            with self.subTest(path=name), self.assertRaises(ToolError):
                self.repo.execute('create_file', {'path':name,'content':'x'})
        with self.assertRaises(ToolError):
            self.repo.execute('create_file', {'path':'binary.txt','content':'x\0y'})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_read_reports_unsupported_encoding(self):
        (self.root/'legacy.txt').write_bytes(b'\xff\xfe')
        with self.assertRaises(ToolError) as error:
            self.files.execute('read_file', {'path':'legacy.txt'})
        self.assertEqual(error.exception.code, 'unsupported_encoding')

    def test_paths_cannot_escape_through_symlinks(self):
        with tempfile.TemporaryDirectory(prefix='orchestra-outside-') as outside:
            try:
                (self.root/'linked').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('OS does not permit creating symlinks for this user')
            for action,args in [('create_file',{'path':'linked/a.txt','content':'x'}),
                                ('replace_text',{'path':'linked/a.txt','old_text':'x','new_text':'y'})]:
                with self.assertRaises(ToolError) as error:
                    self.repo.execute(action,args)
                self.assertEqual(error.exception.code,'path_forbidden')
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_every_git_tool_dispatches_and_failed_multi_file_patch_is_atomic(self):
        def git(*args):
            return subprocess.run(['git','-C',str(self.root),*args], check=True, capture_output=True)
        git('init','-q')
        git('config','user.email','test@example.invalid')
        git('config','user.name','Test')
        (self.root/'a.txt').write_text('old\n')
        (self.root/'b.txt').write_text('other\n')
        git('add','.')
        git('commit','-qm','baseline')
        self.repo=RepositoryTools(self.root)
        self.assertTrue(self.repo.available)
        self.assertIn('status',self.repo.execute('git_status',{}))
        self.assertEqual(self.repo.execute('git_diff',{})['diff'],'')
        self.assertEqual(self.repo.execute('git_log',{})['commits'],1)
        self.assertIn('baseline',self.repo.execute('git_show',{'revision':'HEAD'})['show'])
        diff='diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n'
        bad='diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-wrong\n+broken\n'
        with self.assertRaises(ToolError) as error:
            self.repo.execute('apply_patch',{'patch':diff+bad})
        self.assertEqual(error.exception.code,'patch_check_failed')
        self.assertIn('diagnostic',error.exception.details)
        self.assertEqual((self.root/'a.txt').read_text(),'old\n')
        self.assertEqual((self.root/'b.txt').read_text(),'other\n')
        self.repo.execute('apply_patch',{'patch':diff})
        self.assertIn('+new',self.repo.execute('git_diff',{'path':'a.txt'})['diff'])

    def test_check_failure_output_limit_and_unknown_name(self):
        self.repo.checks={'fail':[sys.executable,'-B','-c', 'import sys; print("x"*60000); sys.exit(2)']}
        result=self.repo.execute('run_check',{'name':'fail'})
        self.assertFalse(result['passed'])
        self.assertEqual(result['returncode'],2)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['output']),self.repo.MAX_OUTPUT)
        with self.assertRaises(ToolError):
            self.repo.execute('run_check',{'name':'arbitrary command'})

    def test_check_timeout_terminates_process(self):
        self.repo.checks={'slow':[sys.executable,'-B','-c','import time; time.sleep(30)']}
        started=time.monotonic()
        with patch.object(self.repo,'CHECK_TIMEOUT',0.2), self.assertRaises(ToolError) as error:
            self.repo.execute('run_check',{'name':'slow'})
        self.assertEqual(error.exception.code,'check_timeout')
        self.assertLess(time.monotonic()-started,15)

    def test_invalid_arguments_are_rejected_before_io(self):
        for name,args in [('create_file',{'path':'x','content':False}),
                          ('create_file',{'path':'x','content':'a','overwrite':True}),
                          ('replace_text',{'path':'x','old_text':5,'new_text':'b'}),
                          ('git_diff',{'context_lines':True}),
                          ('run_check',{'name':[]})]:
            with self.subTest(tool=name), self.assertRaises(ToolError) as error:
                self.repo.execute(name,args)
            self.assertEqual(error.exception.code,'invalid_arguments')
        self.assertEqual(list(self.root.iterdir()),[])

    def test_schema_names_and_json_validation(self):
        definitions=[*FILE_TOOLS,*GIT_TOOLS,BLOCKER_TOOL]
        names=[t['function']['name'] for t in definitions]
        self.assertEqual(len(names),len(set(names)))
        self.assertEqual({t['function']['name'] for t in GIT_TOOLS}, GIT_TOOL_NAMES)
        for raw in ['{broken','[]','null']:
            with self.assertRaises(ToolError):
                parse_arguments(raw)
        tool=next(t for t in GIT_TOOLS if t['function']['name']=='git_diff')
        for bad in [True,1.5,float('nan')]:
            with self.assertRaises(ToolError):
                validate_arguments(tool,{'context_lines':bad})


class RuntimeContracts(unittest.IsolatedAsyncioTestCase):
    call = test_reliability.RuntimeReliability.call

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='orchestra-runtime-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.app=test_reliability.RuntimeReliability.app(self)
        self.app.repository=RepositoryTools(self.root,checks={
            'unit':[sys.executable,'-B','-c','from new import value; assert value == 2']})
        self.app.files=ProjectTools(self.root)
        self.trace=TurnTrace(self.root/'logs','offline acceptance',self.root)
        self.task=self.trace.start_task('director','offline acceptance')

    def response(self,name,args):
        return {'role':'assistant','content':None,'tool_calls':[self.call(name,args)]}

    async def run_responses(self,responses,mutation=False,role='director'):
        self.app.request=AsyncMock(side_effect=responses)
        messages=[]
        result=await self.app.run(role,messages,{'calls':16},{},self.trace,self.task,mutation_requested=mutation)
        self.trace.finish_task(self.task,result['status'])
        self.trace.finish(result['status'])
        return result,messages

    async def test_create_replace_check_and_trace_end_to_end(self):
        result,_=await self.run_responses([
            self.response('create_file',{'path':'new.py','content':'value = 1\n'}),
            self.response('replace_text',{'path':'new.py','old_text':'value = 1','new_text':'value = 2'}),
            self.response('run_check',{'name':'unit'}),
            {'role':'assistant','content':'Changed and checked.'}],mutation=True)
        self.assertEqual(result['status'],'completed')
        self.assertEqual((self.root/'new.py').read_text(),'value = 2\n')
        summary=json.loads((self.trace.directory/'summary.json').read_text())
        self.assertEqual(summary['modified_files'],['new.py'])
        metrics=summary['root_task']['metrics']
        self.assertEqual((metrics['edits_applied'],metrics['files_created'],metrics['checks_run']), (2,1,1))
        self.assertEqual(metrics['checks_failed'],0)
        self.assertEqual(metrics['patches_applied'],0)
        tools=[e for e in self.trace.events if e['event']=='tool_finished']
        self.assertTrue(all(e['ok'] for e in tools))
        self.assertTrue(tools[-1]['result']['passed'])

    async def test_blocker_available_without_git_for_research_and_ends_tools(self):
        result,_=await self.run_responses([
            self.response('report_blocker',{'reason':'missing_information','evidence':' Contract missing. ','next_step':' Provide interface. '}),
            {'role':'assistant','content':'Blocked; contract missing.'}])
        self.assertEqual(result['status'],'partial')
        first=self.app.request.call_args_list[0]
        self.assertIn('report_blocker',[t['function']['name'] for t in first.args[2]])
        self.assertEqual(self.app.request.call_args_list[1].args[8],True)
        self.assertEqual(self.trace.tasks[self.task]['metrics']['blockers_reported'],1)
        self.assertTrue(any(e['event']=='implementation_blocked' and e['evidence']=='Contract missing.' for e in self.trace.events))

    async def test_missing_verification_is_partial(self):
        result,_=await self.run_responses([
            self.response('create_file',{'path':'new.py','content':'value = 2'}),
            {'role':'assistant','content':'Done.'}],mutation=True)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['verification']['status'],'not_run')

    async def test_failed_check_then_repair_and_recheck_completes(self):
        result,_=await self.run_responses([
            self.response('create_file',{'path':'new.py','content':'value = 1'}),
            self.response('run_check',{'name':'unit'}),
            self.response('replace_text',{'path':'new.py','old_text':'value = 1','new_text':'value = 2'}),
            self.response('run_check',{'name':'unit'}),
            {'role':'assistant','content':'Repaired and checked.'}],mutation=True)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['verification']['status'],'passed')
        self.assertEqual(self.trace.tasks[self.task]['metrics']['checks_failed'],1)

    async def test_failed_check_cannot_be_reported_completed(self):
        result,_=await self.run_responses([
            self.response('create_file',{'path':'new.py','content':'value = 1'}),
            self.response('run_check',{'name':'unit'}),
            {'role':'assistant','content':'Done.'}],mutation=True)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['verification']['status'],'failed')

    async def test_edit_invalidates_previous_check(self):
        result,_=await self.run_responses([
            self.response('create_file',{'path':'new.py','content':'value = 2'}),
            self.response('run_check',{'name':'unit'}),
            self.response('replace_text',{'path':'new.py','old_text':'value = 2','new_text':'value = 3'}),
            {'role':'assistant','content':'Done.'}],mutation=True)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['verification']['status'],'stale')

    async def test_blocker_after_edit_still_partial_and_preserves_file(self):
        result,_=await self.run_responses([
            self.response('create_file',{'path':'new.py','content':'value = 1'}),
            self.response('report_blocker',{'reason':'unavailable_capability','evidence':'Required dependency is unavailable.','next_step':'Supply dependency.'}),
            {'role':'assistant','content':'Partial implementation; blocked.'}],mutation=True)
        self.assertEqual(result['status'],'partial')
        self.assertTrue((self.root/'new.py').exists())

    async def test_blocker_mixed_with_edit_performs_neither(self):
        blocked=self.call('report_blocker',{'reason':'unsafe_change','evidence':'Conflicting contract.','next_step':'Resolve contract.'})
        edit=self.call('create_file',{'path':'must_not_exist.py','content':'oops'})
        result,messages=await self.run_responses([
            {'role':'assistant','content':None,'tool_calls':[blocked,edit]},
            {'role':'assistant','content':'No changes made.'}])
        self.assertFalse((self.root/'must_not_exist.py').exists())
        outputs=[json.loads(m['content']) for m in messages if m['role']=='tool']
        self.assertTrue(all(o['error']=='conflicting_actions' for o in outputs))

    async def test_worker_cannot_edit_or_report_blocker(self):
        result,messages=await self.run_responses([
            self.response('create_file',{'path':'must_not_exist.py','content':'oops'}),
            self.response('report_blocker',{'reason':'missing_information','evidence':'Missing','next_step':'Ask'}),
            {'role':'assistant','content':'Research limited.'},
            {'role':'assistant','content':'Research limited.'}],role='worker')
        self.assertFalse((self.root/'must_not_exist.py').exists())
        outputs=[json.loads(m['content']) for m in messages if m['role']=='tool']
        self.assertTrue(all(o['error']=='tool_unavailable' for o in outputs))

    async def test_runtime_enforces_schema_and_records_rejection(self):
        result,messages=await self.run_responses([
            self.response('create_file',{'path':'new.py','content':True}),
            {'role':'assistant','content':'Invalid call; no changes.'}])
        self.assertFalse((self.root/'new.py').exists())
        output=json.loads(next(m['content'] for m in messages if m['role']=='tool'))
        self.assertEqual(output['error'],'invalid_arguments')
        self.assertEqual(self.trace.tasks[self.task]['metrics']['tool_failures'],1)

    async def test_create_and_check_same_response_is_rejected(self):
        result,messages=await self.run_responses([
            {'role':'assistant','content':None,'tool_calls':[
                self.call('create_file',{'path':'new.py','content':'value = 2'}),
                self.call('run_check',{'name':'unit'})]},
            {'role':'assistant','content':'Created, not checked.'}],mutation=True)
        outputs={m['tool_call_id']:json.loads(m['content']) for m in messages if m['role']=='tool'}
        self.assertTrue(outputs['create_file']['ok'])
        self.assertEqual(outputs['run_check']['error'],'check_requires_completed_patch')
        self.assertEqual(result['status'],'partial')


if __name__=='__main__':
    unittest.main()
