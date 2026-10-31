import copy
import json
import tempfile
import unittest
from pathlib import Path
from context_compaction import compact_tool_history, transcript_characters
from tests import test_context_compaction as fixtures


class Economy(unittest.TestCase):
    pair=fixtures.Compaction.pair

    def read(self,key,start,end,total=100,sha='v1',size=2000):
        return self.pair(key,'read_file',{'path':'a.py','start_line':start,'end_line':end},
            {'content':key*size,'content_sha256':sha,'returned_start_line':start,
             'returned_end_line':end,'total_lines':total,'truncated':False})

    def test_complete_current_read_replaces_old_version_and_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[*self.read('old',20,40),*self.read('new',1,100,sha='v2'),
                      *self.pair('r1'),*self.pair('r2')]
            compact_tool_history(messages,Path(folder)/'a.json',threshold=4000,economy=True)
            ids={c['id'] for m in messages for c in m.get('tool_calls',[])}
            self.assertNotIn('old',ids)
            self.assertIn('new',ids)

    def test_partial_new_version_does_not_remove_unknown_surroundings(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[*self.read('old',1,100),*self.read('new',20,40,sha='v2'),
                      *self.pair('r1'),*self.pair('r2')]
            before=copy.deepcopy(messages)
            compact_tool_history(messages,Path(folder)/'a.json',threshold=4000,economy=True)
            self.assertEqual(messages[:4],before[:4])

    def test_verified_small_edits_collapse_and_recent_source_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[]
            for i in range(40):
                messages.extend(self.pair(str(i),'replace_text',
                    {'path':'a.py','old_text':'old'+str(i)+'x'*150,'new_text':'new'+str(i)+'y'*150},
                    {'applied':True,'files':['a.py']}))
            messages.extend(self.read('current',1,100,size=30))
            messages.extend(self.pair('check','run_check',{'name':'unit'},{'passed':True,'output':'OK'}))
            original=copy.deepcopy(messages)
            stats=compact_tool_history(messages,Path(folder)/'a.json',threshold=100000,verified=True,economy=True)
            self.assertIsNotNone(stats)
            self.assertLess(transcript_characters(messages),transcript_characters(original)*0.65)
            self.assertEqual(messages[-4:],original[-4:])
            ids={c['id'] for m in messages for c in m.get('tool_calls',[])}
            results={m['tool_call_id'] for m in messages if m.get('role')=='tool'}
            self.assertEqual(ids,results)
            archived=json.loads((Path(folder)/'a.json').read_text(encoding='utf-8'))['messages']
            self.assertEqual([x['message'] for x in archived],original[:-4])

    def test_small_unverified_edits_are_not_removed_by_new_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[*self.pair('edit','replace_text',{'path':'a.py','old_text':'a','new_text':'b'},
                       {'applied':True,'files':['a.py']}),*self.pair('r1'),*self.pair('r2')]
            original=copy.deepcopy(messages)
            compact_tool_history(messages,Path(folder)/'a.json',threshold=100000,economy=True)
            self.assertEqual(messages,original)

    def test_same_version_covering_read_replaces_old_fragment(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[*self.read('old',20,40),*self.read('new',10,50),*self.pair('r1'),*self.pair('r2')]
            compact_tool_history(messages,Path(folder)/'a.json',threshold=4000,economy=True)
            ids={c['id'] for m in messages for c in m.get('tool_calls',[])}
            self.assertNotIn('old',ids)
            self.assertIn('new',ids)
