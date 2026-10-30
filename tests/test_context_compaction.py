import copy
import json
from pathlib import Path
import tempfile
import unittest
from context_compaction import compact_tool_history, transcript_characters


class Compaction(unittest.TestCase):
    def pair(self, key, name='create_file', args=None, result=None, ok=True):
        return [{'role':'assistant','content':None,'tool_calls':[{'id':key,'type':'function',
            'function':{'name':name,'arguments':json.dumps(args or {'path':'a.py','content':'x'*8000})}}]},
            {'role':'tool','tool_call_id':key,'content':json.dumps({'ok':ok,'result':result or
                {'applied':True,'files':['a.py'],'diff_preview':'x'*2000}})}]

    def test_archives_exact_originals_preserves_pairs_and_recent_history(self):
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'archive.json'
            messages=[{'role':'user','content':'original request'},*self.pair('old'),
                      *self.pair('recent-1'),*self.pair('recent-2')]
            original=copy.deepcopy(messages)
            stats=compact_tool_history(messages,archive,threshold=4000)
            self.assertLess(stats['characters_after'],stats['characters_before']-8000)
            self.assertEqual(messages[0],original[0])
            self.assertEqual(messages[2:],original[3:])
            self.assertNotIn('tool_calls',messages[1])
            self.assertIn('execution_record',json.loads(messages[1]['content']))
            saved=json.loads(archive.read_text())['messages']
            self.assertEqual([item['message'] for item in saved],original[1:3])
            self.assertIsNone(compact_tool_history(messages,archive,threshold=4000))

    def test_errors_failed_checks_and_specification_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[*self.pair('error',ok=False),
                *self.pair('failed','run_check',{'name':'unit'},{'passed':False,'output':'failure'*2000}),
                *self.pair('spec','read_file',{'path':'what_to_do.md'},{'content':'requirements'*2000}),
                *self.pair('r1'),*self.pair('r2')]
            original=copy.deepcopy(messages)
            self.assertIsNone(compact_tool_history(messages,Path(folder)/'a.json',threshold=4000))
            self.assertEqual(messages,original)

    def test_archive_failure_does_not_mutate_history(self):
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'a.json';archive.write_text('existing')
            messages=[*self.pair('old'),*self.pair('r1'),*self.pair('r2')]
            original=copy.deepcopy(messages)
            with self.assertRaises(FileExistsError):
                compact_tool_history(messages,archive,threshold=4000)
            self.assertEqual(messages,original)

    def test_read_metadata_and_check_outcome_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[*self.pair('read','read_file',{'path':'a.py'},
                {'content':'x'*8000,'content_sha256':'abc','next_start_line':201,'truncated':True}),
                *self.pair('check','run_check',{'name':'unit'},
                {'passed':True,'returncode':0,'output':'x'*8000+'Ran 15 tests OK'}),
                *self.pair('r1'),*self.pair('r2')]
            compact_tool_history(messages,Path(folder)/'a.json',threshold=4000)
            read=json.loads(messages[1]['content'])['result']
            self.assertEqual(read['content'],'x'*8000)
            check=json.loads(messages[2]['content'])['execution_record'][0]['result']
            self.assertEqual(read['content_sha256'],'abc')
            self.assertEqual(read['next_start_line'],201)
            self.assertTrue(read['truncated'])
            self.assertTrue(check['passed'])
            self.assertIn('Ran 15 tests OK',check['output_tail'])

    def test_size_includes_tool_arguments(self):
        self.assertGreater(transcript_characters(self.pair('old')),8000)

    def test_unique_code_and_search_survive_even_above_threshold(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[*self.pair('read','read_file',{'path':'a.py'},{'content':'source'*2000}),
                *self.pair('search','search_regex',{'pattern':'bug'},{'matches':'evidence'*1000}),
                *self.pair('r1'),*self.pair('r2')]
            before=copy.deepcopy(messages)
            self.assertIsNone(compact_tool_history(messages,Path(folder)/'a.json',threshold=4000))
            self.assertEqual(messages,before)

    def test_identical_request_with_changed_content_is_not_discarded(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[*self.pair('old','read_file',{'path':'a.py'},{'content':'old'*3000}),
                *self.pair('new','read_file',{'path':'a.py'},{'content':'new'*3000}),
                *self.pair('r1'),*self.pair('r2')]
            before=copy.deepcopy(messages)
            self.assertIsNone(compact_tool_history(messages,Path(folder)/'a.json',threshold=4000))
            self.assertEqual(messages,before)

    def test_exact_duplicate_read_is_removed_but_replacement_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            result={'content':'source'*2000,'content_sha256':'same'}
            messages=[*self.pair('old','read_file',{'path':'a.py'},result),
                *self.pair('new','read_file',{'path':'a.py'},result),*self.pair('r1'),*self.pair('r2')]
            self.assertIsNotNone(compact_tool_history(messages,Path(folder)/'a.json',threshold=4000))
            reads=[m for m in messages if m.get('tool_call_id')=='new']
            self.assertEqual(json.loads(reads[0]['content'])['result']['content'],result['content'])
