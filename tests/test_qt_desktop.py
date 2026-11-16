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
    def test_reasoning_roundtrip_and_model_switch(self):
        import json
        with tempfile.TemporaryDirectory() as temporary:
            data = DesktopData(Path(__file__).resolve().parents[1], temporary)
            data.initialize()
            dialog = SettingsDialog(None, data)
            try:
                dialog.writer_provider.setCurrentText('gpt')
                dialog.model.setCurrentText('gpt-6.1-sol')
                dialog.writer_effort.setCurrentText('medium')
                dialog.review_provider.setCurrentText('gpt')
                dialog.review_model.setCurrentText('gpt-6.1-sol')
                dialog.review_effort.setCurrentText('high')
                dialog.writer_provider.setCurrentText('mimo')
                self.assertFalse(dialog.writer_effort.isEnabled())
                dialog.writer_provider.setCurrentText('gpt')
                dialog.model.setCurrentText('gpt-6.1-sol')
                self.assertEqual(dialog.writer_effort.currentText(), 'medium')
                with patch('qt_settings.QMessageBox.warning') as warning:
                    dialog.save()
                    warning.assert_not_called()
                roles = json.loads(data.config.read_text(encoding='utf-8'))['roles']
                self.assertEqual(roles['writer']['reasoning_effort'], 'medium')
                self.assertEqual(roles['reviewer']['reasoning_effort'], 'high')
            finally:
                dialog.close()
            reopened = SettingsDialog(None, data)
            try:
                self.assertEqual(reopened.writer_effort.currentText(), 'medium')
                self.assertEqual(reopened.review_effort.currentText(), 'high')
                reopened.review_provider.setCurrentText('Same as executor')
                self.assertFalse(reopened.review_effort.isEnabled())
            finally:
                reopened.close()

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_inline_pricing_follows_model_and_keeps_unsaved_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = DesktopData(Path(__file__).resolve().parents[1], temporary)
            data.initialize()
            dialog = SettingsDialog(None, data)
            try:
                dialog.writer_provider.setCurrentText('deepseek')
                model = dialog.model.currentText()
                self.assertIn(f'deepseek / {model}', dialog.pricing_group.title())
                self.assertEqual(dialog.rate_fields['output'].text(), '')
                with self.assertRaisesRegex(ValueError, model):
                    dialog.collect_rates('deepseek', model)
                for key in ('input_cache_miss', 'input_cache_hit', 'output'):
                    dialog.rate_fields[key].setText('0.25')
                dialog.writer_provider.setCurrentText('gpt')
                dialog.writer_provider.setCurrentText('deepseek')
                self.assertEqual(dialog.rate_fields['output'].text(), '0.25')
                dialog.collect_rates('deepseek', model)
                self.assertEqual(dialog.catalog['deepseek'][model]['output'], 0.25)
            finally:
                dialog.close()

    def test_review_toggle_updates_explicit_route_and_displays_output_cost(self):
        import json

        with tempfile.TemporaryDirectory() as temporary:
            data = DesktopData(Path(__file__).resolve().parents[1], temporary)
            data.initialize()
            path = data.root / 'settings.json'
            settings = json.loads(path.read_text(encoding='utf-8'))
            settings['direct']['route'] = ['implement', 'review']
            path.write_text(json.dumps(settings), encoding='utf-8')
            dialog = SettingsDialog(None, data)
            try:
                dialog.limits['direct', 'completion_review_budget_percent'].setText('20')
                dialog.limits['direct', 'completion_review_output_tokens'].setText('1000')
                dialog.rate_fields['output'].setText('10')
                self.assertIn('$0.0200', dialog.review_cost.text())
                dialog.review_enabled.setChecked(False)
                dialog.save()
                saved = json.loads(path.read_text(encoding='utf-8'))['direct']
                self.assertFalse(saved['completion_review_enabled'])
                self.assertEqual(saved['route'], ['implement'])
            finally:
                dialog.close()

    def test_window_restores_draft_and_layout(self):
        with tempfile.TemporaryDirectory() as temporary, asyncio.Runner() as runner:
            root = Path(temporary)
            data = DesktopData(Path(__file__).resolve().parents[1], root / 'data')
            data.initialize()
            window = DesktopWindow(data, runner.get_loop(), root)
            window.show()
            self.qt.processEvents()
            self.assertGreater(window.prompt.height(), window.tabs.height())
            window.toggle_fullscreen()
            self.assertTrue(window.isFullScreen())
            window.toggle_fullscreen()
            self.assertFalse(window.isFullScreen())
            window.tabs.setCurrentIndex(1)
            window.prompt.setPlainText('Draft to restore')
            window.task_split.setSizes([180, 400])
            window.close()

            restored = DesktopWindow(data, runner.get_loop())
            try:
                restored.show()
                self.qt.processEvents()
                self.assertEqual(restored.workspace, root)
                self.assertEqual(restored.tabs.currentIndex(), 1)
                self.assertEqual(restored.prompt.toPlainText(), 'Draft to restore')
                self.assertLess(*restored.task_split.sizes())
            finally:
                restored.close()

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
                artifacts = ['PROJECT_MAP.md', 'ORCHESTRA_JOURNAL.jsonl',
                             'INTERFACE_NOTES.json', '.orchestra-usage.json']
                ignored = window.git('check-ignore', '--', *artifacts, folder=repo).stdout.splitlines()
                self.assertEqual(ignored, ['INTERFACE_NOTES.json', '.orchestra-usage.json'])
                nested = window.make_project(str(repo), 'nested', True)
                self.assertFalse((nested / '.git').exists())

                with self.assertRaisesRegex(ValueError, 'already exists'):
                    window.make_project(str(root), 'with git', True)
                window.register_project(repo)
                self.assertEqual(window.workspace, repo)
                self.assertIn(str(repo), data.load_preferences()['workspaces'])
            finally:
                window.close()

    def test_history_groups_runs_and_puts_time_in_left_column(self):
        from datetime import datetime

        with tempfile.TemporaryDirectory() as temporary, asyncio.Runner() as runner:
            root = Path(temporary)
            data = DesktopData(Path(__file__).resolve().parents[1], root / 'data')
            data.initialize()
            stamps = [datetime(2026, 11, day, hour, 7).astimezone()
                      for day, hour in ((13, 23), (14, 23), (14, 0))]
            runs = [(root / f'run-{i}', {'topic': f'Task {i}', 'started_at': stamp.isoformat()})
                    for i, stamp in enumerate(stamps)]
            with patch.object(data, 'history', return_value={str(root): runs}):
                window = DesktopWindow(data, runner.get_loop(), root)
            try:
                project = window.projects.topLevelItem(0)
                self.assertEqual(project.childCount(), 2)
                latest = project.child(0)
                self.assertEqual(latest.childCount(), 2)
                self.assertEqual((latest.child(0).text(0), latest.child(0).text(1)),
                                 ('00:07', 'Task 2'))
                self.assertEqual(latest.child(1).text(0), '23:07')
                window.select_project(latest.child(0), None)
                self.assertEqual(window.selected_run, runs[2])
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
                on_progress(dict(kind='reasoning', request_id='escaped', model='Sol', text='Start\\'))
                on_progress(dict(kind='reasoning', request_id='escaped', model='Sol', text=r'nLine\tTab'))
                on_progress(dict(kind='reasoning', request_id='req', model='Sol', text='Сводка'))
                on_progress(dict(kind='edit_draft', request_id='req', item_id='edit',
                                 name='create_file', text='черновик'))
                await asyncio.Event().wait()

            client = SimpleNamespace(close=AsyncMock())
            session = SimpleNamespace(new_task=work, engine=SimpleNamespace(clients={'gpt': client}, settings={}))
            window.prompt.setPlainText('Проверить')
            try:
                with patch.object(window, 'session', return_value=session):
                    window.start('new_task')
                window.tick()
                self.assertIn('Start\nLine\tTab', window.reasoning.toPlainText())
                original = window.workspace
                other = original / 'other-project'
                other.mkdir()
                window.register_project(other)
                self.assertTrue(window.projects.isEnabled())
                self.assertTrue(all(button.isEnabled() for button in window.project_actions))
                self.assertEqual(window.workspace, other)
                window.progress(dict(kind='reasoning', request_id='background', model='Sol',
                                     text='Background reasoning'))
                self.assertNotIn('Background reasoning', window.reasoning.toPlainText())
                window.register_project(original)
                self.assertIn('Start\nLine\tTab', window.reasoning.toPlainText())
                self.assertIn('Background reasoning', window.reasoning.toPlainText())
                self.assertIsNotNone(window.task)
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
