import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import ProjectTools
from git_tools import RepositoryTools
from tool_errors import ToolError, model_result
from orchestra import Orchestra, PublicToolError


class ToolContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='orchestra-tools-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = ProjectTools(self.root)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def test_read_default_and_continuation(self):
        self.write('a.txt', '\n'.join(str(i) for i in range(250)))
        first = self.tools.read_file('a.txt')
        self.assertEqual((first['returned_end_line'], first['next_start_line']), (200, 201))
        last = self.tools.read_file('a.txt', first['next_start_line'])
        self.assertEqual(last['returned_end_line'], 250)
        self.assertFalse(last['truncated'])
        self.assertEqual(self.tools.read_file('a.txt', 1, 240)['returned_end_line'], 240)

    def test_character_cap_whole_lines(self):
        self.write('a.txt', ('x' * 9990 + '\n') * 3)
        page = self.tools.read_file('a.txt')
        self.assertEqual(page['returned_end_line'], 2)
        self.assertEqual(page['next_start_line'], 3)
        self.assertLessEqual(len(page['content']), 20000)

    def test_oversized_line_and_empty_file(self):
        self.write('a.txt', 'x' * 20001)
        with self.assertRaises(ToolError) as error:
            self.tools.read_file('a.txt')
        self.assertEqual(error.exception.code, 'line_too_long')
        self.write('empty.txt', '')
        self.assertIsNone(self.tools.read_file('empty.txt')['next_start_line'])

    def test_glob_semantics(self):
        self.assertTrue(self.tools.matches_glob('backend/a.cs', 'backend/**/*.cs'))
        self.assertTrue(self.tools.matches_glob('backend/nested/a.cs', 'backend/**/*.cs'))
        self.assertFalse(self.tools.matches_glob('backend/nested/a.cs', 'backend/*.cs'))
        self.assertTrue(self.tools.matches_glob('frontend/deep/a.vue', '*.vue'))
        self.assertFalse(self.tools.matches_glob('frontend/a.VUE', '*.vue'))

    def test_snapshot_survives_changes_without_rescan(self):
        self.write('a.txt', 'match one\nmatch two\nmatch three')
        first = self.tools.search_text('match', max_results=1)
        self.write('a.txt', 'unrelated')
        with patch.object(self.tools, 'resolve', side_effect=AssertionError('unexpected rescan')):
            second = self.tools.search_text('match', max_results=2, cursor=first['next_cursor'])
        self.assertIn('match two', second['matches'])
        self.assertEqual(second['matches_returned'], 2)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(self.tools.search_text('match')['matches_returned'], 0)

    def test_invalid_expired_and_evicted_cursor(self):
        self.write('a.txt', 'match\nmatch')
        first = self.tools.search_text('match', max_results=1)
        with self.assertRaises(ToolError) as error:
            self.tools.search_text('other', cursor=first['next_cursor'])
        self.assertEqual(error.exception.code, 'invalid_cursor')
        self.tools._search_snapshots[first['snapshot_id']]['created'] -= 301
        with self.assertRaises(ToolError) as error:
            self.tools.search_text('match', cursor=first['next_cursor'])
        self.assertEqual(error.exception.code, 'cursor_expired')
        first = self.tools.search_text('match', max_results=1)
        for i in range(8):
            self.tools.search_text(str(i))
        with self.assertRaises(ToolError) as error:
            self.tools.search_text('match', cursor=first['next_cursor'])
        self.assertEqual(error.exception.code, 'cursor_expired')

    def test_compact_response_retains_trace_statistics(self):
        self.write('a.txt', 'match')
        result = self.tools.search_text('match')
        compact = model_result('search_text', result)
        self.assertNotIn('files_scanned', compact)
        self.assertNotIn('query', compact)
        self.assertEqual(Orchestra.tool_result_summary('search_text', result)['files_scanned'], 1)
        self.assertIn('collection_truncated', compact)

    def test_safe_errors(self):
        for args, code in [({'path': 'missing.txt'}, 'file_not_found'),
                           ({'path': '../outside.txt'}, 'path_forbidden'),
                           ({'path': '.env'}, 'path_forbidden'),
                           ({'path': 'a.txt', 'start_line': 0}, 'invalid_arguments'),
                           ({'unknown': 1}, 'invalid_arguments')]:
            with self.subTest(args=args), self.assertRaises(PublicToolError) as error:
                self.tools.execute('read_file', args)
            self.assertEqual(error.exception.code, code)
            self.assertNotIn(str(self.root), str(error.exception))

    def test_regex_and_path_filter(self):
        self.write('backend/a.cs', 'class A {}')
        self.write('backend/sub/b.cs', 'class B {}')
        result = self.tools.search_regex(r'class [AB]', file_pattern='backend/**/*.cs')
        self.assertEqual(result['matches_returned'], 2)
        with self.assertRaises(ToolError) as error:
            self.tools.search_regex('[')
        self.assertEqual(error.exception.code, 'invalid_regex')

    def test_patch_success_format_and_conflict(self):
        self.write('a.txt', 'old\n')
        repo = RepositoryTools(self.root)
        diff = 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n'
        result = repo.apply_patch(diff)
        self.assertEqual(result['files_changed'], 1)
        self.assertEqual((self.root / 'a.txt').read_text(), 'new\n')
        with self.assertRaises(ToolError) as error:
            repo.apply_patch(diff)
        self.assertEqual(error.exception.code, 'patch_check_failed')
        with self.assertRaises(ToolError) as error:
            repo.apply_patch('\n'.join(diff.splitlines()[1:]) + '\n')
        self.assertEqual(error.exception.code, 'invalid_patch')
        with self.assertRaises(ToolError):
            repo.apply_patch(diff + 'rename from config.local.json\nrename to exposed.txt\n')

    def test_prepare_and_create_commit_require_fresh_snapshot(self):
        self.write('a.txt', 'base\n')
        self.write('b.txt', 'base\n')
        repo = RepositoryTools(self.root)
        repo._run(['init', '-q'])
        repo = RepositoryTools(self.root)
        repo._run(['config', 'user.name', 'Tool Test'])
        repo._run(['config', 'user.email', 'tool-test@example.invalid'])
        repo._run(['add', 'a.txt', 'b.txt'])
        repo._run(['commit', '-qm', 'baseline'])
        self.write('a.txt', 'changed a\n')
        self.write('b.txt', 'changed b\n')
        prepared = repo.prepare_commit()
        self.assertEqual(set(prepared['paths']), {'a.txt', 'b.txt'})
        self.write('b.txt', 'changed after plan\n')
        with self.assertRaises(ToolError) as error:
            repo.create_commit('fix: update a', ['a.txt'], prepared['fingerprint'])
        self.assertEqual(error.exception.code, 'stale_commit_plan')
        prepared = repo.prepare_commit()
        result = repo.create_commit('fix: update a', ['a.txt'], prepared['fingerprint'])
        self.assertEqual(result['paths'], ['a.txt'])
        self.assertIn('b.txt', result['remaining_changes'])
        self.assertEqual(repo._run(['show', '-s', '--format=%s', 'HEAD'])['stdout'].strip(),
                         'fix: update a')


if __name__ == '__main__':
    unittest.main()
