import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from project_data import JOURNAL, MAP, preserve_legacy_checkout_changes, project_data_path
from project_map import HEADER
from project_usage import FILENAME, read_usage, record_usage


class ProjectDataTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('git'), 'Git is required')
    def test_old_generated_edits_are_backed_up_and_switch_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace = base / 'workspace'
            workspace.mkdir()

            def git(*args, check=True):
                return subprocess.run(['git', '-C', str(workspace), *args], check=check,
                                      capture_output=True, encoding='utf-8')

            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            journal = '{"time":"2026-01-01","event":"run_started"}\n'
            (workspace / JOURNAL).write_text(journal, encoding='utf-8')
            (workspace / MAP).write_text(HEADER + 'base', encoding='utf-8')
            (workspace / 'user.txt').write_text('base', encoding='utf-8')
            git('add', '--', JOURNAL, MAP, 'user.txt')
            git('commit', '-qm', 'Base')
            git('switch', '-qc', 'other')
            (workspace / MAP).write_text(HEADER + 'other', encoding='utf-8')
            (workspace / JOURNAL).write_text(journal.replace('run_started', 'run_finished'), encoding='utf-8')
            git('commit', '-qam', 'Other branch')
            git('switch', '-q', 'main')
            updated = journal + '{"time":"2026-01-02","event":"edit"}\n'
            (workspace / JOURNAL).write_text(updated, encoding='utf-8')
            (workspace / MAP).write_text(HEADER + 'local', encoding='utf-8')
            (workspace / 'user.txt').write_text('keep', encoding='utf-8')
            self.assertNotEqual(git('switch', 'other', check=False).returncode, 0)
            storage = project_data_path(base / 'logs', workspace)
            self.assertEqual(preserve_legacy_checkout_changes(workspace, storage, git), [JOURNAL, MAP])
            git('switch', 'other')
            self.assertEqual((storage / JOURNAL).read_text(encoding='utf-8'), updated)
            self.assertEqual(next(storage.glob('legacy-*-' + MAP)).read_text(encoding='utf-8'),
                             HEADER + 'local')
            self.assertEqual((workspace / 'user.txt').read_text(encoding='utf-8'), 'keep')

    def test_existing_usage_is_carried_forward_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / 'workspace'
            entry = {'api_calls': 1, 'reported_calls': 1}
            record_usage(workspace, [entry], [.1])
            storage = project_data_path(Path(temporary) / 'logs', workspace)
            for _ in range(2):
                record_usage(storage, [entry], [.2], legacy_workspace=workspace)
            self.assertAlmostEqual(read_usage(storage)['total']['estimated_usd'], .5)
            self.assertAlmostEqual(read_usage(workspace)['total']['estimated_usd'], .1)
            self.assertTrue((storage / FILENAME).is_file())
