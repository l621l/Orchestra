import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from openai import APITimeoutError
from tests import test_single_model as fixtures


class CompactDefaults(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.SingleModel.setUp
    call=fixtures.SingleModel.call
    response=fixtures.SingleModel.response

    def timeout(self):
        return APITimeoutError(request=SimpleNamespace(method='POST',url='https://offline.invalid'))

    async def test_timeout_preserves_changes_and_stops_chain(self):
        self.app.request=AsyncMock(side_effect=[
            self.response(self.call('create_file',path='value.txt',content='correct')),self.timeout()])
        result=await self.app.run_iterations('original',{},self.trace,self.task)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['stop_reason'],'request_timeout')
        self.assertEqual(result['iterations'],1)
        self.assertEqual(self.app.request.call_count,2)
        self.assertEqual((self.root/'value.txt').read_text(),'correct')
        saved=json.loads((self.trace.directory/'iteration_1.json').read_text())
        self.assertIn('value.txt',saved['runtime']['modified_files'])
        self.assertIn('timed out',saved['report']['summary'])
        self.assertTrue(any(e['event']=='chain_paused_after_timeout' for e in self.trace.events))

    def test_git_history_and_commit_preparation_are_opt_in(self):
        self.app.repository.available=True
        names={t['function']['name'] for t in self.app.available_tools()}
        self.assertTrue({'git_status','git_diff'}<=names)
        self.assertFalse({'git_log','git_show','prepare_commit'}&names)
        self.settings['tools']['git_history_enabled']=True
        names={t['function']['name'] for t in self.app.available_tools()}
        self.assertTrue({'git_log','git_show'}<=names)
        self.assertNotIn('prepare_commit',names)
        self.settings['tools']['allow_git_mutations']=True
        self.assertIn('prepare_commit',{t['function']['name'] for t in self.app.available_tools()})

    def test_exposed_schema_is_smaller_and_keeps_validation_limits(self):
        self.app.repository.available=True
        tools=self.app.available_tools()
        self.assertLess(len(json.dumps(tools,ensure_ascii=False)),10964)
        search=next(t for t in tools if t['function']['name']=='search_text')
        self.assertEqual(search['function']['parameters']['properties']['context_lines']['maximum'],5)
        edit=next(t for t in tools if t['function']['name']=='replace_text')
        self.assertIn('interface_notes',edit['function']['parameters']['properties'])
