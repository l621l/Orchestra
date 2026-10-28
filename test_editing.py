import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from git_tools import RepositoryTools
from tool_errors import ToolError
import test_execution_phase


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = RepositoryTools(self.root)

    def test_crlf_unicode_bom_no_final_newline(self):
        p = self.root/'a.txt'
        p.write_bytes(b'\xef\xbb\xbf' + 'first\r\nстарое\r\nlast'.encode())
        result = self.repo.execute('replace_text', {'path':'a.txt', 'old_text':'first\nстарое', 'new_text':'first\nновое'})
        self.assertTrue(result['applied'])
        self.assertEqual(p.read_bytes(), b'\xef\xbb\xbf' + 'first\r\nновое\r\nlast'.encode())

    def test_rejections_preserve_bytes(self):
        p=self.root/'a.txt'; p.write_bytes(b'aaa\n')
        for old,new,code in [('missing','x','text_not_found'), ('aa','x','text_not_unique'), ('aaa','aaa','no_change')]:
            with self.assertRaises(ToolError) as error:
                self.repo.replace_text('a.txt',old,new)
            self.assertEqual(error.exception.code,code)
            self.assertEqual(p.read_bytes(),b'aaa\n')
        for name in ['../outside.txt', 'config.local.json', '.git/config']:
            with self.assertRaises(ToolError):
                self.repo.replace_text(name,'a','b')

    def test_atomic_replace_failure_leaves_original_and_no_temp(self):
        p=self.root/'a.txt'; p.write_bytes(b'old\n')
        with patch('git_tools.os.replace',side_effect=OSError('simulated')):
            with self.assertRaises(OSError):
                self.repo.replace_text('a.txt','old','new')
        self.assertEqual(p.read_bytes(),b'old\n')
        self.assertEqual([p.name for p in self.root.iterdir()],['a.txt'])


class EditingRuntime(unittest.IsolatedAsyncioTestCase):
    app = test_execution_phase.PhaseRuntime.app
    trace = test_execution_phase.PhaseRuntime.trace
    call = test_execution_phase.PhaseRuntime.call
    response = test_execution_phase.PhaseRuntime.response

    async def test_failed_followup_edit_is_partial_even_after_success(self):
        app=self.app()
        app.repository.execute=MagicMock(side_effect=[
            {'applied':True,'files':['a.txt'],'files_changed':1},
            ToolError('text_not_found','stale fragment')])
        app.request=AsyncMock(side_effect=[
            self.response('replace_text',{'path':'a.txt','old_text':'a','new_text':'b'}),
            self.response('replace_text',{'path':'a.txt','old_text':'stale','new_text':'c'}),
            {'role':'assistant','content':'Only partially implemented'}])
        result=await app.run('director',[],{'calls':16},{},self.trace(),'root',mutation_requested=True)
        self.assertEqual(result['status'],'partial')

    async def test_repeated_patch_failure_switches_to_replacement(self):
        app=self.app()
        app.repository.execute=MagicMock(side_effect=[
            ToolError('patch_check_failed','bad diff'),
            ToolError('patch_check_failed','bad diff'),
            {'applied':True,'files':['a.txt'],'files_changed':1}])
        app.request=AsyncMock(side_effect=[
            self.response('apply_patch',{'patch':'bad1'}),
            self.response('apply_patch',{'patch':'bad2'}),
            self.response('apply_patch',{'patch':'bad3'}),
            self.response('replace_text',{'path':'a.txt','old_text':'a','new_text':'b'}),
            {'role':'assistant','content':'Changed'}])
        messages=[]
        result=await app.run('director',messages,{'calls':16},{},self.trace(),'root',mutation_requested=True)
        self.assertEqual(result['status'],'partial')  # edit succeeded, verification is missing
        self.assertEqual(app.repository.execute.call_count,3)
        third_tools=[t['function']['name'] for t in app.request.call_args_list[2].args[2]]
        self.assertNotIn('apply_patch',third_tools)
        self.assertIn('replace_text',third_tools)
        errors=[json.loads(m['content']).get('error') for m in messages if m['role']=='tool']
        self.assertIn('edit_retry_limit',errors)

    async def test_replacement_and_check_cannot_share_batch(self):
        app=self.app()
        app.request=AsyncMock(side_effect=[
            {'role':'assistant','content':None,'tool_calls':[
                self.call('replace_text',{'path':'a.txt','old_text':'a','new_text':'b'}),
                self.call('run_check',{'name':'unit'})]},
            {'role':'assistant','content':'Changed; check not run'}])
        messages=[]
        await app.run('director',messages,{'calls':16},{},self.trace(),'root',mutation_requested=True)
        self.assertEqual(app.repository.execute.call_count,1)
        errors=[json.loads(m['content']).get('error') for m in messages if m['role']=='tool']
        self.assertIn('check_requires_completed_patch',errors)


if __name__ == '__main__':
    unittest.main()
