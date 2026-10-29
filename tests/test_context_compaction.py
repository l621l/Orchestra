"""Lossless archival and conservative cleanup of completed review rounds."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_compaction import compact_round_reviews
from orchestra import validate_settings
from runtime_settings import load_runtime_settings
from worker_workspaces import WorkerWorkspaces
from tests import test_worker_workspaces as workspace_tests


class Compaction(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.archive = Path(temp.name) / 'context' / 'review.json'

    def pair(self, key, task, ok=True):
        call = {'role': 'assistant', 'content': 'Important unresolved finding.', 'tool_calls': [
            {'id': key, 'type': 'function', 'function': {'name': 'review_task_changes',
             'arguments': json.dumps({'task_id': task, 'path': 'a.py'})}}]}
        result = {'task_id': task, 'diff': '+' + 'x' * 10000, 'fingerprint': 'abc',
                  'changed_paths': ['a.py'], 'truncated': True, 'transfer_limitation': 'Keep this warning.'}
        return [call, {'role': 'tool', 'tool_call_id': key,
                       'content': json.dumps({'ok': ok, 'result': result})}]

    def test_only_matching_diff_is_replaced_after_exact_original_is_archived(self):
        messages = [{'role': 'system', 'content': 'Original specification.'},
                    *self.pair('old', 'closed'), *self.pair('active', 'active'),
                    *self.pair('error', 'closed', ok=False),
                    {'role': 'user', 'content': 'Worker report: unresolved risks.'}]
        original = copy.deepcopy(messages)
        stats = compact_round_reviews(messages, {'closed'}, 'round_1', self.archive)
        self.assertEqual(stats['messages_compacted'], 1)
        self.assertLess(stats['characters_after'], stats['characters_before'] / 3)
        self.assertEqual(json.loads(self.archive.read_text(encoding='utf-8'))['messages'], [original[2]])
        self.assertEqual(messages[:2], original[:2])
        self.assertEqual(messages[3:], original[3:])
        reduced = json.loads(messages[2]['content'])['result']
        full = json.loads(original[2]['content'])['result']
        for key in full.keys() - {'diff'}:
            self.assertEqual(reduced[key], full[key])
        self.assertEqual(reduced['review_again']['arguments'], {'task_id': 'closed', 'path': 'a.py'})
        self.assertEqual(messages[2]['tool_call_id'], original[2]['tool_call_id'])
        self.assertIsNone(compact_round_reviews(messages, {'closed'}, 'round_1', self.archive))

    def test_archive_failure_leaves_entire_history_unchanged(self):
        messages = self.pair('old', 'closed')
        original = copy.deepcopy(messages)
        self.archive.parent.mkdir()
        self.archive.write_text('Existing archive.', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            compact_round_reviews(messages, {'closed'}, 'round_1', self.archive)
        self.assertEqual(messages, original)
        self.assertEqual(self.archive.read_text(), 'Existing archive.')

    def test_small_reviews_and_unrelated_rounds_do_not_create_archive(self):
        messages = self.pair('old', 'closed')
        original = copy.deepcopy(messages)
        self.assertIsNone(compact_round_reviews(messages, {'other'}, 'round_2', self.archive))
        self.assertIsNone(compact_round_reviews(messages, {'closed'}, 'round_1', self.archive, 20000))
        self.assertEqual(messages, original)
        self.assertFalse(self.archive.exists())

    def test_switch_and_threshold_are_validated(self):
        settings = load_runtime_settings('settings.json', 'comparison')
        self.assertTrue(settings['execution']['compact_completed_round_reviews'])
        self.assertFalse(load_runtime_settings('settings.json')['execution']['compact_completed_round_reviews'])
        for key, value in [('compact_completed_round_reviews', 'true'),
                           ('compact_review_min_characters', True),
                           ('compact_review_min_characters', 0)]:
            invalid = copy.deepcopy(settings)
            invalid['execution'][key] = value
            with self.assertRaises(ValueError):
                validate_settings(invalid)


class RoundIntegration(unittest.IsolatedAsyncioTestCase):
    setUp = workspace_tests.ComparisonRuntime.setUp
    call = workspace_tests.ComparisonRuntime.call
    response = workspace_tests.ComparisonRuntime.response
    prepare = workspace_tests.ComparisonRuntime.prepare

    async def test_disabled_switch_never_touches_history(self):
        original_load = workspace_tests.load_runtime_settings
        def disabled(*args, **kwargs):
            settings = original_load(*args, **kwargs)
            settings['execution']['compact_completed_round_reviews'] = False
            return settings
        with patch.object(workspace_tests, 'load_runtime_settings', disabled), \
                patch('orchestra.compact_round_reviews') as compact:
            await workspace_tests.ComparisonRuntime.test_two_rounds_choose_whole_candidates_and_rebase_next_pair(self)
        compact.assert_not_called()

    async def test_archive_error_does_not_undo_commit_or_prevent_next_round(self):
        with patch('orchestra.compact_round_reviews', side_effect=OSError('Disk unavailable.')):
            await workspace_tests.ComparisonRuntime.test_two_rounds_choose_whole_candidates_and_rebase_next_pair(self)
        events = [e for e in self.trace.events if e['event'] == 'context_compaction_skipped']
        self.assertEqual(len(events), 2)

    async def test_two_promotions_archive_old_reviews_and_keep_next_round_working(self):
        original_review = WorkerWorkspaces.review
        def long_review(manager, *args, **kwargs):
            result = original_review(manager, *args, **kwargs)
            # Simulate a large tool response while retaining real frozen patches,
            # fingerprints, promotion checks and commits in the existing fixture.
            result['diff'] += '\n' + ('+review context\n' * 1000)
            return result
        with patch.object(WorkerWorkspaces, 'review', long_review):
            await workspace_tests.ComparisonRuntime.test_two_rounds_choose_whole_candidates_and_rebase_next_pair(self)
        events = [e for e in self.trace.events if e['event'] == 'context_compacted']
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e['messages_compacted'] == 2 for e in events))
        for event in events:
            self.assertLess(event['characters_after'], event['characters_before'])
            saved = json.loads(Path(event['archive']).read_text(encoding='utf-8'))
            self.assertEqual(len(saved['messages']), 2)
