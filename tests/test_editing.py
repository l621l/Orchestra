import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from git_tools import RepositoryTools
from tool_errors import ToolError

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
