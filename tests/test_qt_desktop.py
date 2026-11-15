import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from orchestra_qt import QApplication, DesktopWindow, SettingsDialog
except ModuleNotFoundError as error:
    if error.name != 'PySide6':
        raise
    QApplication = None

from desktop_data import DesktopData


@unittest.skipIf(QApplication is None, 'Install requirements-qt.txt for Qt tests')
class QtDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_git_graph_selects_commit_under_label(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from qt_git_graph import CommitRow, GitGraph, layout_commits

        history = '\n'.join('\0'.join(row) for row in (
            ('merge', 'left right', 'm', ' (HEAD -> main)', 'Merge work'),
            ('left', 'base', 'l', '', 'Main change'),
            ('right', 'base', 'r', ' (feature)', 'Feature change'),
            ('base', '', 'b', '', 'Initial commit'),
        ))
        rows, lanes = layout_commits(history)
        self.assertEqual(lanes, 2)
        self.assertEqual(set(rows[0][2]), {(0, 0), (0, 1)})
        graph = GitGraph(history, head='left')
        graph.resize(800, 220)
        graph.show()
        self.qt.processEvents()
        try:
            commit_rows = {row.commit: row for row in graph.scene().items()
                           if isinstance(row, CommitRow)}
            QTest.mouseMove(graph.viewport(), QPoint(200, 17))
            self.assertTrue(commit_rows['merge'].hovered)
            QTest.mouseMove(graph.viewport(), QPoint(200, 51))
            self.assertFalse(commit_rows['merge'].hovered)
            self.assertTrue(commit_rows['left'].hovered)
            QTest.mouseMove(graph.viewport(), QPoint(200, 180))
            self.assertFalse(commit_rows['left'].hovered)
            QTest.mouseClick(graph.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(200, 17))
            self.assertEqual([row.commit for row in graph.scene().selectedItems()], ['merge'])
            self.assertEqual([row.commit for row in graph.scene().items()
                              if isinstance(row, CommitRow) and row.is_head], ['left'])
        finally:
            graph.close()

    def test_missing_project_is_hidden_until_directory_returns(self):
        with tempfile.TemporaryDirectory() as temporary, asyncio.Runner() as runner:
            root = Path(temporary)
            project = root / 'project'
            data = DesktopData(Path(__file__).resolve().parents[1], root / 'data')
            data.initialize()
            data.save_preferences({'workspaces': [str(project)]})
            window = DesktopWindow(data, runner.get_loop(), project)
            try:
                self.assertEqual(window.projects.topLevelItemCount(), 0)
                self.assertIsNone(window.workspace)
                project.mkdir()
                window.reload_projects()
                self.assertEqual(window.projects.topLevelItemCount(), 1)
                self.assertEqual(window.workspace, project)
            finally:
                window.close()

    def test_create_project_initializes_git_only_when_requested_and_needed(self):
        with tempfile.TemporaryDirectory() as temporary, asyncio.Runner() as runner:
            root = Path(temporary)
            data = DesktopData(Path(__file__).resolve().parents[1], root / 'data')
            data.initialize()
            window = DesktopWindow(data, runner.get_loop())
            try:
                plain = window.make_project(str(root), 'plain', False)
                self.assertTrue(plain.is_dir())
                self.assertFalse((plain / '.git').exists())

                repo = window.make_project(str(root), 'with git', True)
                self.assertEqual(window.git('symbolic-ref', '--short', 'HEAD', folder=repo).stdout.strip(), 'main')
                artifacts = ['PROJECT_MAP.md', 'INTERFACE_NOTES.json', 'ORCHESTRA_JOURNAL.jsonl']
                ignored = window.git('check-ignore', '--', *artifacts, folder=repo).stdout.splitlines()
                self.assertEqual(ignored, artifacts)
                nested = window.make_project(str(repo), 'nested', True)
                self.assertFalse((nested / '.git').exists())

                with self.assertRaisesRegex(ValueError, 'уже существует'):
                    window.make_project(str(root), 'with git', True)
                window.register_project(repo)
                self.assertEqual(window.workspace, repo)
                self.assertIn(str(repo), data.load_preferences()['workspaces'])
            finally:
                window.close()

    def test_origin_web_link_handles_ssh_and_removes_http_credentials(self):
        from orchestra_qt import git_web_url

        self.assertEqual(git_web_url('git@github.com:owner/repo.git'), 'https://github.com/owner/repo')
        self.assertEqual(git_web_url('https://user:secret@example.org:8443/group/repo.git'),
                         'https://example.org:8443/group/repo')
        self.assertIsNone(git_web_url('C:/projects/repo.git'))

    def test_task_stream_and_close_during_run(self):
        with tempfile.TemporaryDirectory() as temporary, asyncio.Runner() as runner:
            root = Path(temporary)
            data = DesktopData(Path(__file__).resolve().parents[1], root / 'data')
            data.initialize()
            window = DesktopWindow(data, runner.get_loop(), root)
            dialog = SettingsDialog(window, data)
            dialog.writer_provider.setCurrentText('gpt')
            dialog.model.setCurrentText('gpt-6.1-sol')
            dialog.save()
            self.assertTrue(data.config.is_file())

            async def work(_text, on_progress):
                on_progress(dict(kind='reasoning', request_id='req', model='Sol', text='Сводка'))
                on_progress(dict(kind='edit_draft', request_id='req', item_id='edit',
                                 name='create_file', text='черновик'))
                await asyncio.Event().wait()

            client = SimpleNamespace(close=AsyncMock())
            session = SimpleNamespace(new_task=work, engine=SimpleNamespace(clients={'gpt': client}))
            window.prompt.setPlainText('Проверить')
            try:
                with patch.object(window, 'session', return_value=session):
                    window.start('new_task')
                window.tick()
                self.assertIn('Сводка', window.reasoning.toPlainText())
                self.assertIn('черновик', window.reasoning.toPlainText())
                window.close()
                for _ in range(5):
                    window.tick()
                    if window.task is None:
                        break
                self.assertIsNone(window.task)
                client.close.assert_awaited_once()
            finally:
                window.close()
