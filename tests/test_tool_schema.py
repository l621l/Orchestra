import copy
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from git_tools import GIT_TOOLS, RepositoryTools
from tool_contracts import validate_arguments
from tool_errors import ToolError
from tool_schema import compact_tools
from tools import FILE_TOOLS
from tests import test_pipeline as fixtures


class ToolSchema(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.Alternating.setUp
    call = fixtures.Alternating.call

    def test_defaults_exclude_git_even_with_git_flags_enabled(self):
        self.app.repository.available = True
        self.app.repository.checks = {'unit':['python','-V']}
        self.app.settings['tools'].update(git_history_enabled=True,allow_git_mutations=True)
        tools = self.app.available_tools()
        self.assertEqual({t['function']['name'] for t in tools},
                         {'list_files','read_file','search_text','search_regex','create_file','replace_text','run_check'})
        self.assertLess(len(json.dumps(tools,ensure_ascii=False)),5000)

    def test_compaction_preserves_every_validation_constraint_and_original(self):
        original = copy.deepcopy(FILE_TOOLS+GIT_TOOLS)
        compact = compact_tools(original)
        self.assertEqual(original,FILE_TOOLS+GIT_TOOLS)

        def strip(value):
            if isinstance(value,dict):
                return {k:strip(v) for k,v in value.items() if k!='description'}
            if isinstance(value,list):
                return [strip(v) for v in value]
            return value

        self.assertEqual(strip(compact),strip(original))
        self.assertLess(len(json.dumps(compact)),len(json.dumps(original)))
        edit = next(t for t in compact if t['function']['name']=='replace_text')
        validate_arguments(edit,{'path':'a.py','old_text':'a','new_text':'b'})
        with self.assertRaises(ToolError):
            validate_arguments(edit,{'path':'a.py','old_text':'a'})

    def test_disabled_repository_does_not_invoke_git_for_init_or_edits(self):
        with tempfile.TemporaryDirectory() as directory, patch('git_tools.subprocess.run',side_effect=AssertionError('Git must not run')):
            repo = RepositoryTools(directory,git_enabled=False)
            self.assertFalse(repo.available)
            self.assertTrue(repo.create_file('value.txt','before')['applied'])
            self.assertTrue(repo.replace_text('value.txt','before','after')['applied'])
            with self.assertRaises(ToolError) as error:
                repo._run(['status'])
            self.assertEqual(error.exception.code,'git_disabled')

    async def test_unexposed_git_and_patch_cannot_execute(self):
        self.app.repository.git_status = MagicMock(side_effect=AssertionError('disabled'))
        self.app.repository.apply_patch = MagicMock(side_effect=AssertionError('disabled'))
        self.app.request = AsyncMock(side_effect=[
            {'role':'assistant','content':None,'tool_calls':[
                self.call('git_status'), self.call('apply_patch',patch='irrelevant')]},
            {'role':'assistant','content':'stopped'}])
        messages=[]
        await self.app.run('worker',messages,{'calls':2},{},self.trace,self.task)
        outputs=[json.loads(m['content']) for m in messages if m.get('role')=='tool']
        self.assertEqual([o['error'] for o in outputs],['tool_unavailable','tool_unavailable'])
        self.app.repository.git_status.assert_not_called()
        self.app.repository.apply_patch.assert_not_called()
