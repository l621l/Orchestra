"""The final model request must receive actual check results, even at zero spare calls."""
import json
import time
import asyncio
import sys
import unittest
from unittest.mock import patch

from tests import test_base_tools


class VerificationReserve(unittest.IsolatedAsyncioTestCase):
    setUp = test_base_tools.RuntimeContracts.setUp
    call = test_base_tools.RuntimeContracts.call
    response = test_base_tools.RuntimeContracts.response

    async def run_short(self, responses, calls=3, **kwargs):
        from unittest.mock import AsyncMock
        self.app.request = AsyncMock(side_effect=responses)
        messages, budget = [], {'calls': calls}
        result = await self.app.run('director', messages, budget, {}, self.trace,
                                    self.task, mutation_requested=True, **kwargs)
        return result, messages, budget

    async def test_automatic_check_without_extra_model_call(self):
        result, messages, budget = await self.run_short([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
            {'role': 'assistant', 'content': 'Done and verified.'}])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual(self.app.request.call_count, 2)
        self.assertEqual(budget['calls'], 1)
        events = [e for e in self.trace.events if e['event'] == 'tool_finished']
        self.assertEqual([e['tool'] for e in events], ['create_file', 'run_check'])
        self.assertTrue(events[-1]['result']['passed'])
        evidence = [m for m in messages if 'Automatic configured check result' in (m.get('content') or '')]
        self.assertEqual(len(evidence), 1)

    async def test_penultimate_edit_rechecks_before_last_model_response(self):
        result, messages, budget = await self.run_short([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
            self.response('replace_text', {'path': 'new.py', 'old_text': 'value = 2', 'new_text': 'value = 3'}),
            {'role': 'assistant', 'content': 'Modified, but verification failed.'}])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['verification']['status'], 'failed')
        self.assertEqual(result['verification']['workspace_revision'], 2)
        checks = [e for e in self.trace.events if e['event'] == 'automatic_verification_finished']
        self.assertEqual([e['passed'] for e in checks], [True, False])
        self.assertTrue(self.app.request.call_args_list[-1].args[8])
        self.assertEqual(budget['calls'], 0)

    async def test_failure_can_be_repaired_in_penultimate_call(self):
        result, _, _ = await self.run_short([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 1'}),
            self.response('replace_text', {'path': 'new.py', 'old_text': 'value = 1', 'new_text': 'value = 2'}),
            {'role': 'assistant', 'content': 'Fixed and verified.'}])
        self.assertEqual(result['status'], 'completed')
        checks = [e for e in self.trace.events if e['event'] == 'automatic_verification_finished']
        self.assertEqual([e['passed'] for e in checks], [False, True])

    async def test_explicit_check_is_not_repeated_on_same_revision(self):
        result, _, _ = await self.run_short([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
            self.response('run_check', {'name': 'unit'}),
            {'role': 'assistant', 'content': 'Verified.'}], calls=4)
        self.assertEqual(result['status'], 'completed')
        checks = [e for e in self.trace.events if e['event'] == 'tool_finished' and e['tool'] == 'run_check']
        self.assertEqual(len(checks), 1)

    async def test_last_call_still_has_check_evidence(self):
        result, _, budget = await self.run_short([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
            {'role': 'assistant', 'content': 'Verified.'}], calls=2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual(budget['calls'], 0)

    async def test_no_checks_does_not_fabricate_a_pass(self):
        self.app.repository.checks = {}
        result, _, _ = await self.run_short([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
            {'role': 'assistant', 'content': 'No checks configured.'}])
        self.assertEqual(result['verification']['status'], 'not_configured')
        self.assertFalse(any(e['event'] == 'automatic_verification_finished' for e in self.trace.events))

    async def test_deadline_leaves_report_time_and_does_not_claim_verified(self):
        with patch.object(self.app.repository, 'run_check') as check:
            result, _, _ = await self.run_short([
                self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
                {'role': 'assistant', 'content': 'Changes unverified.'}],
                turn_deadline=time.monotonic() + 10)
        check.assert_not_called()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['verification']['status'], 'not_run')
        self.assertTrue(any(e['event'] == 'automatic_verification_skipped' for e in self.trace.events))

    async def test_explicit_blocker_does_not_start_automatic_checks(self):
        result, _, _ = await self.run_short([
            self.response('create_file', {'path': 'new.py', 'content': 'value = 2'}),
            self.response('report_blocker', {'reason': 'missing_information',
                'evidence': 'Required behavior is unspecified.', 'next_step': 'Supply the contract.'}),
            {'role': 'assistant', 'content': 'Blocked.'}], calls=4)
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(any(e['event'] == 'automatic_verification_finished' for e in self.trace.events))

    async def test_internal_check_timeout_terminates_process(self):
        self.app.repository.checks['slow'] = [sys.executable, '-B', '-c', 'import time; time.sleep(10)']
        from tool_errors import ToolError
        with self.assertRaises(ToolError) as error:
            await asyncio.to_thread(self.app.repository.execute,
                'run_check', {'name': 'slow'}, check_timeout=0.05)
        self.assertEqual(error.exception.code, 'check_timeout')
