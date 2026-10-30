import copy
import difflib
import json
from pathlib import Path
import tempfile
import unittest
from git_tools import RepositoryTools
from context_compaction import compact_tool_history
from tool_errors import ToolError
from tests import test_context_compaction as fixtures

MARKER='[Archived '+ 'executed payload. Inspect current project files before reusing or editing this content.]'


class MarkerRegression(unittest.TestCase):
    pair=fixtures.Compaction.pair

    def test_archived_history_contains_no_executable_placeholder_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            messages=[*self.pair('old'),*self.pair('recent1'),*self.pair('recent2')]
            original=copy.deepcopy(messages)
            compact_tool_history(messages,Path(directory)/'archive.json',threshold=4000)
            self.assertNotIn(MARKER,json.dumps(messages))
            self.assertEqual(messages[0]['role'],'user')
            self.assertTrue(json.loads(messages[0]['content'])['program_history_record'])
            calls={c['id'] for m in messages for c in m.get('tool_calls',[])}
            results={m['tool_call_id'] for m in messages if m.get('role')=='tool'}
            self.assertEqual(calls,results)
            self.assertNotIn('old',calls)
            self.assertEqual(messages[1:],original[2:])
            archived=json.loads((Path(directory)/'archive.json').read_text())
            self.assertEqual([i['message'] for i in archived['messages']],original[:2])

    def test_mixed_success_and_error_batch_stays_exactly_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            good=self.pair('good');bad=self.pair('bad',ok=False)
            group=good[0]
            group['tool_calls']+=bad[0]['tool_calls']
            messages=[group,good[1],bad[1],*self.pair('r1'),*self.pair('r2')]
            before=copy.deepcopy(messages)
            compact_tool_history(messages,Path(directory)/'a.json',threshold=4000,verified=True)
            self.assertEqual(messages[:3],before[:3])

    def test_pending_call_is_never_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            messages=[self.pair('pending')[0],*self.pair('r1'),*self.pair('r2')]
            before=copy.deepcopy(messages)
            compact_tool_history(messages,Path(directory)/'a.json',threshold=4000,verified=True)
            self.assertEqual(messages[0],before[0])
            self.assertEqual(messages[0]['tool_calls'][0]['id'],'pending')

    def test_all_write_tools_reject_marker_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repo=RepositoryTools(root)
            (root/'a.py').write_text('old\n')
            diff='diff --git a/a.py b/a.py\n'+''.join(difflib.unified_diff(
                ['old\n'],[MARKER+'\n'],fromfile='a/a.py',tofile='b/a.py'))
            attempts=[('create_file',{'path':'new.py','content':MARKER}),
                ('replace_text',{'path':'a.py','old_text':'old','new_text':MARKER}),
                ('apply_patch',{'patch':diff})]
            for name,args in attempts:
                with self.subTest(tool=name),self.assertRaises(ToolError) as caught:
                    repo.execute(name,args)
                self.assertEqual(caught.exception.code,'archive_marker_not_source')
                self.assertEqual((root/'a.py').read_text(),'old\n')
                self.assertFalse((root/'new.py').exists())

    def test_repairing_existing_marker_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repo=RepositoryTools(root)
            (root/'a.py').write_text(MARKER)
            result=repo.execute('replace_text',{'path':'a.py','old_text':MARKER,'new_text':'print("repaired")\n'})
            self.assertTrue(result['applied'])
            self.assertNotIn(MARKER,(root/'a.py').read_text())
