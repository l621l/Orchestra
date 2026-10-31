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

    def test_repeat_edit_reports_existing_new_text_without_writing(self):
        p=self.root/'a.txt'; p.write_bytes(b'header\r\nvalue = 2\r\nfooter\r\n')
        before=p.read_bytes()
        with self.assertRaises(ToolError) as error:
            self.repo.replace_text('a.txt','value = 1','value = 2')
        self.assertEqual(error.exception.code,'text_not_found')
        self.assertTrue(error.exception.details['new_text_present'])
        self.assertIn('value = 2',error.exception.details['diagnostic'])
        self.assertIn('presence alone does not prove',str(error.exception))
        self.assertEqual(p.read_bytes(),before)

    def test_nearby_current_fragment_is_a_hint_not_a_fuzzy_edit(self):
        p=self.root/'a.txt';p.write_text('head\n    player_speed = 7\ntail\n',encoding='utf-8')
        before=p.read_bytes()
        with self.assertRaises(ToolError) as error:
            self.repo.replace_text('a.txt','    player_speed = 6','    player_speed = 8')
        self.assertFalse(error.exception.details['new_text_present'])
        self.assertIn('player_speed = 7',error.exception.details['diagnostic'])
        self.assertLessEqual(len(error.exception.details['diagnostic']),1400)
        self.assertEqual(p.read_bytes(),before)

    def test_deletion_and_unrelated_missing_fragment_do_not_claim_already_applied(self):
        p=self.root/'a.txt';p.write_text('nothing related here\n',encoding='utf-8')
        with self.assertRaises(ToolError) as error:
            self.repo.replace_text('a.txt','missing','')
        self.assertFalse(error.exception.details['new_text_present'])
        self.assertNotIn('diagnostic',error.exception.details)
