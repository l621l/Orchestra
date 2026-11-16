import json
import difflib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from orchestra_core.workspace.git_tools import RepositoryTools
from orchestra_core.workspace.interface_notes import FILE, InterfaceNotes
from orchestra_core.workspace.project_map import ProjectMap, HEADER
from orchestra_core.workspace.tool_errors import ToolError
from orchestra_core.workspace.tool_contracts import validate_arguments
from orchestra_core.workspace.git_tools import GIT_TOOLS

SOURCE = "from dataclasses import dataclass\n@dataclass\nclass World:\n    mode: str = 'classic'\n\ndef new_game(mode='classic') -> World:\n    return World(mode=mode)\n"


class InterfaceMemory(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.repo=RepositoryTools(self.root)
        self.note={'path':'logic.py','symbol':'World.mode',
                   'note':'Selected mode; initialized by new_game and used during restart.'}

    def create(self, notes=None):
        args={'path':'logic.py','content':SOURCE}
        if notes is not None: args['interface_notes']=notes
        return self.repo.execute('create_file',args)

    def test_same_edit_records_note_and_actual_declarations(self):
        result=self.create([self.note])
        self.assertTrue(result['applied'])
        self.assertEqual(result['interface_memory']['notes_saved'],1)
        text=(self.root/'PROJECT_MAP.md').read_text(encoding='utf-8')
        self.assertIn("mode: str",text)
        self.assertIn("def new_game(mode='classic') -> World",text)
        self.assertIn('MODEL NOTE @ recorded source version World.mode',text)
        doc=json.loads((self.root/FILE).read_text())
        self.assertEqual(len(doc['notes'][0]['source_hashes']['logic.py']),64)

    def test_source_change_stales_note_and_explicit_note_refreshes_it(self):
        self.create([self.note])
        result=self.repo.execute('replace_text',{'path':'logic.py','old_text':"mode: str = 'classic'",
            'new_text':"mode: str = 'rush'"})
        self.assertEqual(result['interface_memory']['notes_stale'],1)
        self.assertIn('STALE MODEL NOTE World.mode',(self.root/'PROJECT_MAP.md').read_text())
        result=self.repo.execute('replace_text',{'path':'logic.py','old_text':"mode: str = 'rush'",
            'new_text':"mode: str = 'classic'",'interface_notes':[self.note]})
        self.assertEqual(result['interface_memory']['notes_stale'],0)
        self.assertEqual(result['interface_memory']['notes_current'],1)

    def test_related_file_change_invalidates_persistent_note(self):
        (self.root/'main.py').write_text('def run(): pass\n')
        self.create([{**self.note,'related_paths':['main.py']}])
        self.repo.execute('replace_text',{'path':'main.py','old_text':'pass','new_text':'return 1'})
        notes=InterfaceNotes(self.repo.policy).view()
        self.assertEqual(notes[0]['status'],'stale')
        self.assertEqual(notes[0]['changed_sources'],['main.py'])
        self.assertIn('STALE',ProjectMap(self.repo.policy).refresh()['context'])

    def test_failed_edit_does_not_save_notes(self):
        self.create()
        with self.assertRaises(ToolError):
            self.repo.execute('replace_text',{'path':'logic.py','old_text':'missing','new_text':'other',
                                             'interface_notes':[self.note]})
        self.assertFalse((self.root/FILE).exists())

    def test_unknown_symbol_and_outside_paths_are_not_recorded(self):
        result=self.create([{**self.note,'symbol':'World.imagined'},
                            {**self.note,'related_paths':['../outside.py']}])
        self.assertTrue(result['applied'])
        self.assertEqual(result['interface_memory']['notes_saved'],0)
        self.assertTrue(result['warnings'])
        self.assertFalse((self.root/FILE).exists())

    def test_notes_storage_failure_does_not_turn_edit_into_failure(self):
        with patch('orchestra_core.workspace.interface_notes.atomic_text',side_effect=OSError('disk error')):
            result=self.create([self.note])
        self.assertTrue(result['applied'])
        self.assertTrue((self.root/'logic.py').exists())
        self.assertTrue(result['warnings'])

    def test_user_owned_files_are_not_overwritten(self):
        (self.root/'PROJECT_MAP.md').write_text('User map')
        (self.root/FILE).write_text('{"my":"notes"}')
        result=self.create([self.note])
        self.assertTrue(result['applied'])
        self.assertEqual((self.root/'PROJECT_MAP.md').read_text(),'User map')
        self.assertEqual((self.root/FILE).read_text(),'{"my":"notes"}')
        self.assertTrue(result['warnings'])

    def test_syntax_error_replaces_old_interface_and_stales_note(self):
        self.create([self.note])
        (self.root/'logic.py').write_text('class World(\n')
        inventory=self.repo.interface_map.refresh()
        self.assertIn('SyntaxError',inventory['context'])
        self.assertNotIn('def new_game',inventory['context'])
        self.assertEqual(inventory['notes_stale'],1)

    def test_metadata_limits_validate_before_mutation(self):
        with self.assertRaises(ToolError):
            self.create([self.note]*9)
        self.assertFalse((self.root/'logic.py').exists())

    def test_parser_never_imports_source_and_excludes_local_variables(self):
        source="raise RuntimeError('must never import')\nVALUE: int = 1\ndef f(x, /, *, flag=False):\n    local = 1\n    return x\nclass C:\n    def __init__(self):\n        self.name = 'c'\n"
        result=self.repo.execute('create_file',{'path':'a.py','content':source})
        self.assertTrue(result['applied'])
        text=(self.root/'PROJECT_MAP.md').read_text()
        self.assertIn('VALUE: int',text)
        self.assertIn('name: assigned on self',text)
        self.assertIn('def f(x, /, *, flag=False)',text)
        self.assertNotIn('local',text)

    def test_map_context_is_bounded_and_detects_external_edits(self):
        self.create([self.note])
        (self.root/'logic.py').write_text(SOURCE.replace('mode: str','mode: object'))
        inventory=self.repo.interface_map.refresh(context_limit=1000)
        self.assertLessEqual(len(inventory['context']),1000)
        self.assertEqual(inventory['notes_stale'],1)
        self.assertIn('mode: object',(self.root/'PROJECT_MAP.md').read_text())

    def test_apply_patch_updates_memory_after_whole_patch(self):
        self.create()
        diff = 'diff --git a/logic.py b/logic.py\n' + ''.join(difflib.unified_diff(
            SOURCE.splitlines(keepends=True), SOURCE.replace("mode: str = 'classic'", "mode: str = 'rush'").splitlines(keepends=True),
            fromfile='a/logic.py',tofile='b/logic.py'))
        result=self.repo.execute('apply_patch',{'patch':diff,'interface_notes':[self.note]})
        self.assertTrue(result['applied'])
        self.assertEqual(result['interface_memory']['notes_saved'],1)
        self.assertIn("mode: str",(self.root/'PROJECT_MAP.md').read_text())

    def test_disabling_memory_keeps_edit_but_saves_no_notes(self):
        self.repo.interface_memory_enabled=False
        result=self.create([self.note])
        self.assertTrue(result['applied'])
        self.assertFalse((self.root/FILE).exists())
        self.assertFalse((self.root/'PROJECT_MAP.md').exists())
        self.assertTrue(result['warnings'])
