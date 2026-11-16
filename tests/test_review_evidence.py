import copy
import json
import unittest

from orchestra_core.runtime.completion_review import review_evidence_complete
from orchestra_core.models.model_routing import role_settings, reasoning_levels
from orchestra_core.models.responses_transport import request_payload
from types import SimpleNamespace


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.argv = ['{python}', '-c', 'import app; assert app.value == 2']
        self.review = {'findings': [], 'limitations': [],
                       'checks': [{'argv': self.argv, 'kind': 'independent', 'outcome': 'passed'}],
                       'coverage': [{'requirement': 'MAIL-01', 'status': 'verified', 'evidence': 'app.py plus assertion'}]}
        self.messages = self.action('run_command', {'argv': self.argv},
                                    {'qualifies_as_verification': True, 'passed': True})

    def action(self, name, args, value):
        return [{'role': 'assistant', 'tool_calls': [{'id': 'call', 'function': {
            'name': name, 'arguments': json.dumps(args)}}]},
            {'role': 'tool', 'tool_call_id': 'call', 'content': json.dumps({'ok': True, 'result': value})}]

    def test_real_independent_check_and_complete_coverage_pass(self):
        self.assertTrue(review_evidence_complete(self.review, self.messages, 'MAIL-01: required'))

    def test_baseline_or_invented_command_does_not_pass(self):
        for key, value in [('kind', 'baseline'), ('argv', ['python', 'imaginary.py'])]:
            review = copy.deepcopy(self.review)
            review['checks'][0][key] = value
            self.assertFalse(review_evidence_complete(review, self.messages, 'MAIL-01'))

    def test_missing_or_unverified_requirement_does_not_pass(self):
        self.assertFalse(review_evidence_complete(self.review, self.messages, 'MAIL-01 MAIL-02'))
        self.review['coverage'][0]['status'] = 'unverified'
        self.assertFalse(review_evidence_complete(self.review, self.messages, 'MAIL-01'))

    def test_edit_invalidates_previous_check(self):
        self.messages += self.action('replace_text', {}, {'applied': True})
        self.assertFalse(review_evidence_complete(self.review, self.messages, 'MAIL-01'))
        self.messages += self.action('run_command', {'argv': self.argv}, {'qualifies_as_verification': True})
        self.assertTrue(review_evidence_complete(self.review, self.messages, 'MAIL-01'))


class ReasoningTests(unittest.TestCase):
    def test_legacy_review_budget_migrates_to_percentage(self):
        from orchestra_core.runtime.orchestra_config import normalize_settings
        old = {'direct': {'completion_review_calls': 4, 'max_task_cost_usd': 1}}
        current = normalize_settings(old)
        self.assertEqual(current['direct']['completion_review_budget_percent'], 20)
        self.assertNotIn('completion_review_calls', current['direct'])
        current['direct']['completion_review_budget_percent'] = 30
        self.assertEqual(normalize_settings(current)['direct']['completion_review_budget_percent'], 30)
        self.assertEqual(old['direct']['completion_review_calls'], 4)

    def test_roles_reach_payload_independently_and_default_is_omitted(self):
        config = {'providers': {'gpt': {}}, 'roles': {
            'writer': {'provider': 'gpt', 'model': 'gpt-6-astra', 'reasoning_effort': 'medium'},
            'reviewer': {'provider': 'gpt', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}}}
        app = SimpleNamespace(role_settings=lambda role: role_settings(config, role), settings={})
        for role, expected in [('writer', 'medium'), ('reviewer', 'high')]:
            self.assertEqual(request_payload(app, role, [], [], '', max_output_tokens=8192)['reasoning']['effort'], expected)
        config['roles']['writer']['reasoning_effort'] = 'default'
        self.assertNotIn('reasoning', request_payload(app, 'writer', [], [], '', max_output_tokens=8192))
        del config['roles']['reviewer']
        self.assertEqual(app.role_settings('reviewer')['reasoning_effort'], 'default')

    def test_unsupported_effort_is_rejected(self):
        self.assertEqual(reasoning_levels('mimo', 'mimo-v2.6-pro'), ())
        config = {'providers': {'gpt': {}}, 'roles': {'writer': {
            'provider': 'gpt', 'model': 'gpt-4o', 'reasoning_effort': 'high'}}}
        with self.assertRaisesRegex(ValueError, 'Unsupported reasoning'):
            role_settings(config, 'writer')
