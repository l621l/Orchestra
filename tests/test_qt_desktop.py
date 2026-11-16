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

from orchestra_core.desktop.desktop_data import DesktopData


@unittest.skipIf(QApplication is None, 'Install requirements-qt.txt for Qt tests')
class QtDesktopTests(unittest.TestCase):
    def test_task_prompt_arrows_keep_multiline_navigation_and_restore_draft(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QTextCursor
        from PySide6.QtTest import QTest
        from orchestra_qt import TaskPrompt

        prompt = TaskPrompt()
        prompt.set_history(['Recent task', 'Older task'])
        prompt.setPlainText('First line\nSecond line')
        prompt.moveCursor(QTextCursor.MoveOperation.End)
        QTest.keyClick(prompt, Qt.Key.Key_Up)
        self.assertEqual(prompt.textCursor().blockNumber(), 0)
        self.assertEqual(prompt.toPlainText(), 'First line\nSecond line')
        QTest.keyClick(prompt, Qt.Key.Key_Up)
        self.assertEqual(prompt.toPlainText(), 'Recent task')
        QTest.keyClick(prompt, Qt.Key.Key_Up)
        self.assertEqual(prompt.toPlainText(), 'Older task')
        QTest.keyClick(prompt, Qt.Key.Key_Down)
        self.assertEqual(prompt.toPlainText(), 'Recent task')
        QTest.keyClick(prompt, Qt.Key.Key_Down)
        self.assertEqual(prompt.toPlainText(), 'First line\nSecond line')
        prompt.close()

    def test_pricing_remembers_a_model_per_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = DesktopData(Path(__file__).resolve().parents[1], temporary)
            data.initialize()
            dialog = SettingsDialog(None, data, global_only=True)
            for provider, model in [('gpt', 'gpt-6-luna'), ('mimo', 'mimo-v2.6-pro')]:
                dialog.writer_provider.setCurrentText(provider)
                dialog.model.setCurrentText(model)
            dialog.writer_provider.setCurrentText('gpt')
            self.assertEqual(dialog.model.currentText(), 'gpt-6-luna')
            dialog.save()
            reopened = SettingsDialog(None, data, global_only=True)
            try:
                reopened.writer_provider.setCurrentText('gpt')
                self.assertEqual(reopened.model.currentText(), 'gpt-6-luna')
                reopened.writer_provider.setCurrentText('mimo')
                self.assertEqual(reopened.model.currentText(), 'mimo-v2.6-pro')
            finally:
                reopened.close()

    def test_catalog_checkboxes_filter_pricing_and_both_project_roles(self):
        from PySide6.QtCore import Qt
        from orchestra_core.workspace.project_data import SETTINGS, project_data_path

        with tempfile.TemporaryDirectory() as temporary:
            data = DesktopData(Path(__file__).resolve().parents[1], temporary)
            data.initialize()
            global_dialog = SettingsDialog(None, data, global_only=True)
            tree = global_dialog.catalog_tree
            for row in range(tree.topLevelItemCount()):
                provider = tree.topLevelItem(row)
                provider.setCheckState(0, Qt.CheckState.Checked if provider.text(0) == 'gpt'
                                       else Qt.CheckState.Unchecked)
                if provider.text(0) == 'gpt':
                    for index in range(provider.childCount()):
                        model = provider.child(index)
                        model.setCheckState(0, Qt.CheckState.Checked if model.text(0) == 'gpt-6.1-sol'
                                            else Qt.CheckState.Unchecked)
            self.assertEqual(global_dialog.writer_provider.currentText(), 'gpt')
            self.assertEqual([global_dialog.model.itemText(i) for i in range(global_dialog.model.count())],
                             ['gpt-6.1-sol'])
            global_dialog.save()
            reopened = SettingsDialog(None, data, global_only=True)
            self.assertEqual(reopened.writer_provider.count(), 1)
            self.assertEqual([reopened.model.itemText(i) for i in range(reopened.model.count())],
                             ['gpt-6.1-sol'])
            reopened.close()
            path = project_data_path(data.logs, Path(temporary)) / SETTINGS
            dialog = SettingsDialog(None, data, project_path=path)
            self.assertEqual(dialog.writer_provider.count(), 1)
            self.assertEqual(dialog.writer_provider.currentIndex(), -1)
            dialog.writer_provider.setCurrentText('gpt')
            dialog.review_provider.setCurrentText('gpt')
            self.assertEqual([dialog.model.itemText(i) for i in range(dialog.model.count())], ['gpt-6.1-sol'])
            self.assertEqual([dialog.review_model.itemText(i) for i in range(dialog.review_model.count())],
                             ['gpt-6.1-sol'])
            dialog.close()

    def test_opening_click_release_does_not_select_a_combo_item(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from orchestra_core.desktop.qt_settings import SettingsComboBox

        combo = SettingsComboBox()
        combo.addItems(['First', 'Second'])
        combo.show()
        try:
            combo.showPopup()
            self.qt.processEvents()
            view = combo.view()
            position = QPointF(view.visualRect(combo.model().index(1, 0)).center())
            button = Qt.MouseButton.LeftButton

            def send(event_type, buttons):
                event = QMouseEvent(event_type, position, view.viewport().mapToGlobal(position),
                                    button, buttons, Qt.KeyboardModifier.NoModifier)
                self.qt.sendEvent(view.viewport(), event)

            send(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
            self.assertTrue(view.isVisible())
            self.assertEqual(combo.currentIndex(), 0)
            send(QEvent.Type.MouseButtonPress, button)
            send(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
            self.assertEqual(combo.currentIndex(), 1)
            self.assertFalse(view.isVisible())
        finally:
            combo.close()

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
                with patch('orchestra_core.desktop.qt_settings.QMessageBox.warning') as warning:
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

    def test_project_settings_isolate_models_limits_debug_and_git(self):
        import json
        from orchestra_core.runtime.orchestra_app import OrchestraApp
        from orchestra_core.workspace.project_data import SETTINGS, project_data_path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = DesktopData(Path(__file__).resolve().parents[1], root / 'data')
            data.initialize()
            first = root / 'first'
            second = root / 'second'
            first.mkdir()
            second.mkdir()
            global_dialog = SettingsDialog(None, data, global_only=True)
            global_dialog.limits['api', 'timeout_seconds'].setText('123')
            global_dialog.save()
            self.assertTrue(data.config.is_file())
            path = project_data_path(data.logs, first) / SETTINGS
            dialog = SettingsDialog(None, data, project_path=path)
            dialog.writer_provider.setCurrentText('gpt')
            dialog.model.setCurrentText('gpt-6.1-sol')
            dialog.limits['direct', 'max_task_cost_usd'].setText('0.42')
            dialog.review_enabled.setChecked(False)
            flags = dialog.git_flags
            flags['git_tools_enabled'].setChecked(True)
            flags['allow_git_mutations'].setChecked(True)
            flags['allow_git_push'].setChecked(True)
            flags['allow_git_mutations'].setChecked(False)
            self.assertFalse(flags['allow_git_push'].isEnabled())
            self.assertFalse(flags['allow_git_push'].isChecked())
            dialog.save()
            self.assertTrue(path.is_file())
            local = json.loads(path.read_text(encoding='utf-8'))
            self.assertNotIn('providers', local)
            self.assertNotIn('api', local['settings'])
            self.assertFalse(local['settings']['tools']['allow_git_push'])
            a = OrchestraApp.from_directory(data.root, first, logs_path=data.logs).engine
            b = OrchestraApp.from_directory(data.root, second, logs_path=data.logs).engine
            self.assertEqual(a.role_settings('writer')['model'], 'gpt-6.1-sol')
            self.assertEqual(b.role_settings('writer')['provider'], 'mimo')
            self.assertEqual(a.options['max_task_cost_usd'], 0.42)
            self.assertNotEqual(a.options['max_task_cost_usd'], b.options['max_task_cost_usd'])
            self.assertEqual(a.settings['api']['timeout_seconds'], 123)
            self.assertEqual(b.settings['api']['timeout_seconds'], 123)
            self.assertFalse(a.options['completion_review_enabled'])

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

    def test_task_prompt_follows_project_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as temporary, asyncio.Runner() as runner:
            root = Path(temporary)
            data = DesktopData(Path(__file__).resolve().parents[1], root / 'data')
            data.initialize()
            first, second = root / 'first', root / 'second'
            first.mkdir()
            second.mkdir()
            data.save_preferences({'workspaces': [str(first), str(second)]})

            def select(window, project):
                item = next(window.projects.topLevelItem(index)
                            for index in range(window.projects.topLevelItemCount())
                            if window.projects.topLevelItem(index).toolTip(1) == str(project))
                window.projects.setCurrentItem(item)

            window = DesktopWindow(data, runner.get_loop(), first)
            try:
                window.prompt.setPlainText('First task\nMore detail')
                select(window, second)
                self.assertEqual(window.prompt.toPlainText(), '')
                window.prompt.setPlainText('Second draft')
                select(window, first)
                self.assertEqual(window.prompt.toPlainText(), 'First task\nMore detail')
                window.prompt.clear()
                select(window, second)
                self.assertEqual(window.prompt.toPlainText(), 'Second draft')
            finally:
                window.close()

            restored = DesktopWindow(data, runner.get_loop())
            try:
                self.assertEqual(restored.prompt.toPlainText(), 'Second draft')
                select(restored, first)
                self.assertEqual(restored.prompt.toPlainText(), 'First task\nMore detail')
            finally:
                restored.close()

    def test_git_graph_selects_commit_under_label(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from orchestra_core.desktop.qt_git_graph import CommitRow, GitGraph, layout_commits

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
                self.assertTrue(window.start_button.isEnabled())
                self.assertFalse(window.stop_button.isEnabled())
                second_client = SimpleNamespace(close=AsyncMock())

                async def other_work(_text, on_progress):
                    on_progress(dict(kind='reasoning', request_id='req', model='Sol', text='Other project'))
                    await asyncio.Event().wait()

                second_session = SimpleNamespace(
                    new_task=other_work, engine=SimpleNamespace(clients={'gpt': second_client}, settings={}))
                window.prompt.setPlainText('Check other project')
                with patch.object(window, 'session', return_value=second_session) as create_session:
                    window.start('new_task')
                    window.start('new_task')
                    create_session.assert_called_once()
                window.tick()
                first_task = window.runs[original].task
                self.assertFalse(first_task.done())
                self.assertIn('Other project', window.reasoning.toPlainText())
                window.progress(dict(kind='reasoning', request_id='background', model='Sol',
                                     text='Background reasoning'), original)
                self.assertNotIn('Background reasoning', window.reasoning.toPlainText())
                window.stop()
                for _ in range(5):
                    window.tick()
                    if window.task is None:
                        break
                self.assertIsNone(window.task)
                self.assertFalse(first_task.done())
                second_client.close.assert_awaited_once()
                window.register_project(original)
                self.assertIn('Start\nLine\tTab', window.reasoning.toPlainText())
                self.assertIn('Background reasoning', window.reasoning.toPlainText())
                self.assertNotIn('Other project', window.reasoning.toPlainText())
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
