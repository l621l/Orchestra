import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from git_tools import RepositoryTools
from tools import ProjectTools
from tool_errors import ToolError, model_result


class ToolPolish(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = ProjectTools(self.root)
        self.repo = RepositoryTools(self.root)

    def test_read_hash_covers_original_bytes_and_rejects_stale_edits(self):
        path = self.root/'a.txt'
        original = 'first\r\nстарое\r\n'.encode('utf-8')
        path.write_bytes(original)
        read = self.files.read_file('a.txt', start_line=2, end_line=2)
        self.assertEqual(read['content_sha256'], hashlib.sha256(original).hexdigest())
        result = self.repo.execute('replace_text', {'path':'a.txt', 'old_text':'старое',
            'new_text':'новое', 'expected_sha256':read['content_sha256']})
        self.assertEqual(result['content_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        before = path.read_bytes()
        with self.assertRaises(ToolError) as caught:
            self.repo.replace_text('a.txt', 'новое', 'lost', expected_sha256=read['content_sha256'])
        self.assertEqual(caught.exception.code, 'file_changed')
        self.assertEqual(path.read_bytes(), before)

    def test_empty_file_and_range_beyond_eof(self):
        (self.root/'a.txt').write_text('')
        self.assertEqual(self.files.read_file('a.txt')['total_lines'], 0)
        with self.assertRaises(ToolError) as caught:
            self.files.read_file('a.txt', start_line=2)
        self.assertEqual(caught.exception.details['total_lines'], 0)
        (self.root/'a.txt').write_text('line')
        with self.assertRaises(ToolError):
            self.files.read_file('a.txt', start_line=2)

    def test_search_snippet_contains_distant_match_with_context_and_unicode(self):
        (self.root/'a.txt').write_text('ß'*600 + 'NEEDLE' + 'z'*600, encoding='utf-8')
        for context in [0, 1]:
            for mode in ['literal', 'regex']:
                with self.subTest(context=context, mode=mode):
                    result = (self.files.search_text('needle', context_lines=context) if mode=='literal'
                              else self.files.search_regex('needle', context_lines=context))
                    self.assertIn('NEEDLE', result['matches'])
                    self.assertLess(len(result['matches']), 400)

    def test_noisy_check_retains_traceback_and_success_output(self):
        self.repo.checks = {'noisy': [sys.executable, '-B', '-c',
            "import sys; print('START'); print('x'*60000); print('TRACEBACK_END'); sys.exit(1)"],
            'short': [sys.executable, '-B', '-c', "print('OK')"]}
        result = self.repo.run_check('noisy')
        self.assertFalse(result['passed'])
        self.assertTrue(result['truncated'])
        self.assertGreater(result['omitted_bytes'], 0)
        self.assertIn('START', result['output'])
        self.assertIn('TRACEBACK_END', result['output'])
        self.assertLessEqual(len(result['output']), self.repo.MAX_OUTPUT)
        short = self.repo.run_check('short')
        self.assertTrue(short['passed'])
        self.assertFalse(short['truncated'])
        self.assertEqual(short['output'].strip(), 'OK')

    def test_timeout_retains_check_diagnostics(self):
        self.repo.checks = {'slow': [sys.executable, '-B', '-u', '-c',
            "import time; print('before timeout', flush=True); time.sleep(30)"]}
        with self.assertRaises(ToolError) as caught:
            self.repo.run_check('slow', timeout_seconds=1)
        self.assertEqual(caught.exception.code, 'check_timeout')
        self.assertIn('before timeout', caught.exception.details['output'])

    def test_failed_cleanup_does_not_mask_original_replace_failure(self):
        (self.root/'a.txt').write_text('old')
        with patch('git_tools.os.replace', side_effect=OSError('replace failed')),             patch('git_tools.os.unlink', side_effect=PermissionError('cleanup failed')):
            with self.assertRaisesRegex(OSError, 'replace failed'):
                self.repo.replace_text('a.txt','old','new')
        self.assertEqual((self.root/'a.txt').read_text(), 'old')

    def test_edit_response_is_compact_without_mutating_original_result(self):
        full = {'applied':True, 'files':['new.txt'], 'diff':'x'*12000,
                'diff_truncated':False, 'warnings':[], 'status':'large status'}
        result = model_result('create_file', full)
        self.assertNotIn('status', result)
        self.assertNotIn('diff', result)
        self.assertNotIn('diff_preview',result)
        self.assertNotIn('diff_truncated',result)
        self.assertEqual(len(full['diff']), 12000)

    def test_empty_git_history_is_not_an_error(self):
        subprocess.run(['git','init','-q',str(self.root)], check=True, capture_output=True)
        result = RepositoryTools(self.root).execute('git_log', {})
        self.assertEqual(result['commits'], 0)
        self.assertFalse(result['truncated'])
