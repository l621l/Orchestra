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
