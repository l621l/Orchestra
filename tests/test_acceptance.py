"""Acceptance gates must not mistake passing old checks for reviewed coverage."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from acceptance import validate_acceptance, criterion_catalog
from tool_errors import ToolError
from runtime_settings import load_runtime_settings
from tests import test_worker_workspaces as fixtures


class AcceptanceContract(unittest.TestCase):
    def test_short_ids_do_not_depend_on_repeating_descriptions(self):
        criteria = ['A very long requirement with punctuation: `--flag` and JSON.', 'README launch instructions']
        self.assertEqual([c['id'] for c in criterion_catalog(criteria)], ['C1', 'C2'])
        inspected = []
        result = validate_acceptance(criteria, [
            {'id': 'C1', 'check': 'unit', 'evidence': 'test_flag passed'},
            {'id': 'C2', 'files': ['README.md'], 'evidence': 'Launch instructions match code'}],
            ['unit'], review_file=inspected.append)
        self.assertEqual(len(result), 2)
        self.assertEqual(inspected, ['README.md'])

    def test_document_review_needs_no_fabricated_check(self):
        evidence = [{'id': 'C1', 'files': ['README.md'], 'evidence': 'Examples inspected'}]
        self.assertEqual(validate_acceptance(['docs'], evidence, [], review_file=lambda path: None), evidence)
        with self.assertRaises(ToolError) as error:
            validate_acceptance(['docs'], [{'id': 'C1', 'check': 'documentation review',
                                          'evidence': 'Read README'}], ['unit'])
        self.assertEqual(error.exception.code, 'acceptance_check_unavailable')
        self.assertEqual(error.exception.details['configured_checks'], ['unit'])
        self.assertIn('files', error.exception.details['examples'][1])

    def test_missing_duplicate_and_unknown_evidence_are_rejected(self):
        first = {'id': 'C1', 'check': 'unit', 'evidence': 'test_existing'}
        for evidence in ([first], [first, first], [dict(first, id='C99')],
                         [dict(first, check='imaginary')], [dict(first, evidence=' ')]):
            with self.subTest(evidence=evidence), self.assertRaises(ToolError):
                validate_acceptance(['existing behavior', 'new behavior'], evidence, ['unit'])


class AcceptanceRuntime(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.EditingWorkerRuntime.setUp
    prepare = fixtures.EditingWorkerRuntime.prepare
    call = fixtures.EditingWorkerRuntime.call
    response = fixtures.EditingWorkerRuntime.response

    async def candidate(self, documentation=False):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        self.app.repository.checks = {'unit': [sys.executable, '-B', '-c',
                                              'from new import value; assert value == 2']}

        # Worker instances are copies of app; use the runtime's actual tool route
        # via its scripted model instead of writing into the director repository.
        calls = {}
        async def response(*args, **kwargs):
            label = args[7]
            calls[label] = calls.get(label, 0) + 1
            if calls[label] == 1:
                if documentation:
                    return {'role': 'assistant', 'content': None, 'tool_calls': [
                        self.call('create_file', {'path': 'new.py', 'content': 'value = 2\n'}),
                        self.call('create_file', {'path': 'README.md', 'content': 'Run the configured unit check.\n'})]}
                return self.response('create_file',
                    {'path': 'new.py', 'content': 'value = 2\n'})
            return {'role': 'assistant', 'content': 'Implemented; edge-case coverage remains missing.'}
        self.app.request = AsyncMock(side_effect=response)
        started = await scheduler.start_comparison('Add one value.', 3, 500, self.task,
            acceptance_checks=['value equals two', 'README checked' if documentation else 'edge cases tested'])
        self.assertEqual([row['id'] for row in started['acceptance_checks']], ['C1', 'C2'])
        await scheduler.wait(timeout_seconds=20)
        ids = [item['task_id'] for item in started['workers']]
        reviews = [scheduler.workspaces.review(key) for key in ids]
        return scheduler, started, ids[0], reviews[0]['fingerprint']

    async def test_automated_and_document_review_promote_together(self):
        scheduler, started, key, fingerprint = await self.candidate(documentation=True)
        result = scheduler.promote_comparison(started['round_id'], key, fingerprint,
            'Value and README', 'Tests and documentation inspected', [
                {'id': 'C1', 'check': 'unit', 'evidence': 'Configured assertion passed'},
                {'id': 'C2', 'files': ['README.md'], 'evidence': 'Run instructions match the check'}])
        self.assertTrue(result['promoted'])
        self.assertTrue(result['checks'][0]['passed'])
        self.assertTrue((self.root / 'README.md').is_file())

    async def test_review_paths_must_exist_and_stay_inside_candidate(self):
        scheduler, started, key, fingerprint = await self.candidate(documentation=True)
        for path in ['missing.md', '../README.md', '.git/config', '.']:
            with self.subTest(path=path), self.assertRaises(ToolError):
                scheduler.promote_comparison(started['round_id'], key, fingerprint,
                    'Value', 'Review', [
                        {'id': 'C1', 'check': 'unit', 'evidence': 'Assertion passed'},
                        {'id': 'C2', 'files': [path], 'evidence': 'Inspected'}])
            self.assertFalse((self.root / 'new.py').exists())

    async def test_review_evidence_cannot_bypass_failing_configured_checks(self):
        scheduler, started, key, fingerprint = await self.candidate(documentation=True)
        self.app.repository.checks = {'unit': [sys.executable, '-B', '-c', 'raise SystemExit(1)']}
        result = scheduler.promote_comparison(started['round_id'], key, fingerprint,
            'Value', 'Inspected source and docs', [
                {'id': 'C1', 'files': ['new.py'], 'evidence': 'Value inspected'},
                {'id': 'C2', 'files': ['README.md'], 'evidence': 'Instructions inspected'}])
        self.assertFalse(result['promoted'])
        self.assertTrue(result['rolled_back'])
        self.assertFalse((self.root / 'new.py').exists())

    async def test_green_check_and_completed_worker_cannot_bypass_missing_criterion(self):
        scheduler, started, key, fingerprint = await self.candidate()
        head = self.app.repository._run(['rev-parse', 'HEAD'])['stdout']
        evidence = [{'id': 'C1', 'check': 'unit', 'evidence': 'unit imports value'}]
        with self.assertRaises(ToolError) as error:
            scheduler.promote_comparison(started['round_id'], key, fingerprint, 'value', 'Looks fine', evidence)
        self.assertEqual(error.exception.code, 'acceptance_incomplete')
        self.assertEqual(self.app.repository._run(['rev-parse', 'HEAD'])['stdout'], head)
        self.assertFalse((self.root / 'new.py').exists())
        self.assertEqual(scheduler.active_round_id, started['round_id'])

    async def test_checkpoint_preserves_patch_and_baseline_then_next_round_uses_baseline(self):
        scheduler, started, key, fingerprint = await self.candidate()
        head = self.app.repository._run(['rev-parse', 'HEAD'])['stdout'].strip()
        result = scheduler.promote_comparison(started['round_id'], key, fingerprint,
            'partial value', 'Missing edge-case tests', [], decision='checkpoint')
        self.assertFalse(result['promoted'])
        self.assertEqual(result['acceptance_status'], 'pending')
        artifact = json.loads(Path(result['artifact_file']).read_text(encoding='utf-8'))
        self.assertIn('value = 2', artifact['patch'])
        self.assertFalse((self.root / 'new.py').exists())
        self.assertEqual(self.app.repository._run(['rev-parse', 'HEAD'])['stdout'].strip(), head)
        self.assertIsNone(scheduler.active_round_id)
        self.app.run = AsyncMock(return_value={'answer': 'No work', 'status': 'partial', 'result_source': 'final'})
        next_round = await scheduler.start_comparison('A narrower task.', 2, 500, self.task,
            acceptance_checks=['one tested behavior'])
        await scheduler.wait(timeout_seconds=20)
        self.assertEqual(next_round['checkpoint']['head'], head)

    async def test_budget_error_reports_allocatable_pair_without_creating_workers(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        scheduler.shared_budget['calls'] = 15
        with self.assertRaises(ToolError) as error:
            await scheduler.start_comparison('Too much.', 6, 500, self.task,
                acceptance_checks=['tested behavior'])
        self.assertEqual(error.exception.code, 'batch_worker_budget_exceeded')
        self.assertEqual(error.exception.details['max_calls_per_worker'], 4)
        self.assertEqual(scheduler.tasks, {})

    async def test_empty_contract_starts_no_workers(self):
        scheduler = self.prepare()
        with self.assertRaises(ToolError) as error:
            await scheduler.start_comparison('Unspecified checks.', 3, 500, self.task,
                                              acceptance_checks=[])
        self.assertEqual(error.exception.code, 'acceptance_criteria_required')
        self.assertEqual(scheduler.tasks, {})

    async def test_checkpoint_through_model_dispatch_returns_saved_artifact(self):
        scheduler, started, key, fingerprint = await self.candidate()
        head = self.app.repository._run(['rev-parse', 'HEAD'])['stdout']
        checkpoint_call = self.response('promote_candidate', {
            'round_id': started['round_id'], 'task_id': key, 'fingerprint': fingerprint,
            'message': 'Partial work', 'rationale': 'Missing edge-case tests',
            'decision': 'checkpoint', 'acceptance': []})
        sent = False
        async def respond(*args, **kwargs):
            nonlocal sent
            if not sent:
                sent = True
                return checkpoint_call
            return {'role': 'assistant', 'content': 'Partial candidate saved; baseline unchanged.'}
        self.app.request = AsyncMock(side_effect=respond)
        await self.app.run('director', [], scheduler.shared_budget, {}, self.trace, self.task,
                          scheduler=scheduler, max_delegations=2, mutation_requested=True)
        events = [e for e in self.trace.events if e['event'] == 'tool_finished'
                  and e['tool'] == 'promote_candidate']
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]['ok'])
        self.assertEqual(events[0]['result']['acceptance_status'], 'pending')
        self.assertTrue(Path(events[0]['result']['artifact_file']).is_file())
        self.assertEqual(self.app.repository._run(['rev-parse', 'HEAD'])['stdout'], head)
