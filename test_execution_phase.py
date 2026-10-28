import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import test_reliability
from execution_phase import ImplementationPhase, validate_blocker
from tool_errors import ToolError


class PhaseLimits(unittest.TestCase):
    def phase(self, enabled=True):
        return ImplementationPhase({'implementation_reserve_calls': 4,
                                    'implementation_max_research_calls': 6,
                                    'implementation_research_seconds': 180,
                                    'implementation_max_read_calls': 2}, enabled)

    def test_each_transition(self):
        p = self.phase(); p.update(4)
        self.assertEqual(p.reason, 'remaining_calls')
        p = self.phase(); p.research_calls = 6; p.update(10)
        self.assertEqual(p.reason, 'research_calls')
        p = self.phase()
        with patch('execution_phase.time.monotonic', return_value=p.deadline):
            p.update(10)
        self.assertEqual(p.reason, 'research_time')

    def test_audit_is_not_forced_to_edit(self):
        p = self.phase(False); p.research_calls = 100; p.update(1)
        self.assertFalse(p.active)

    def test_focused_reads_and_banned_tools(self):
        p = self.phase(); p.enter('test')
        with self.assertRaises(ToolError):
            p.admit('search_text', {'query': 'x'})
        with self.assertRaises(ToolError):
            p.admit('read_file', {'path': 'x'})
        for _ in range(2):
            p.admit('read_file', {'path': 'x', 'start_line': 1, 'end_line': 120})
        with self.assertRaises(ToolError) as error:
            p.admit('read_file', {'path': 'x', 'start_line': 1, 'end_line': 2})
        self.assertEqual(error.exception.code, 'implementation_read_limit')
        p.admit('apply_patch', {'patch': 'test'})

    def test_blocker_requires_evidence(self):
        with self.assertRaises(ToolError):
            validate_blocker({'reason': 'missing_information', 'evidence': '', 'next_step': 'Ask'})


class PhaseRuntime(unittest.IsolatedAsyncioTestCase):
    app = test_reliability.RuntimeReliability.app
    trace = test_reliability.RuntimeReliability.trace
    call = test_reliability.RuntimeReliability.call

    def response(self, name, args):
        return {'role': 'assistant', 'content': None, 'tool_calls': [self.call(name, args)]}

    async def test_six_research_requests_then_enforced_implementation(self):
        app = self.app()
        app.files.execute = MagicMock(return_value={'tree': 'example'})
        app.request = AsyncMock(side_effect=[
            *[self.response('list_files', {'path': '.'}) for _ in range(6)],
            self.response('search_text', {'query': 'ignored phase'}),
            self.response('apply_patch', {'patch': 'supported change'}),
            {'role': 'assistant', 'content': 'Changed; no tests run.'}])
        messages, trace = [], self.trace()
        result = await app.run('director', messages, {'calls': 16}, {}, trace, 'root', mutation_requested=True)
        self.assertEqual(result['status'], 'partial')  # configured check was not run
        self.assertEqual(app.files.execute.call_count, 6)
        seventh = app.request.call_args_list[6]
        self.assertFalse(seventh.kwargs['thinking_enabled'])
        self.assertNotIn('search_text', [t['function']['name'] for t in seventh.args[2]])
        rejected = [json.loads(m['content']) for m in messages if m['role'] == 'tool' and m['tool_call_id'] == 'search_text']
        self.assertEqual(rejected[0]['error'], 'research_closed')

    async def test_length_recovers_with_editing_tools(self):
        app = self.app()
        app.request = AsyncMock(side_effect=[
            {'role': 'assistant', 'content': None, '_truncated': True, '_partial': False},
            self.response('apply_patch', {'patch': 'small patch'}),
            {'role': 'assistant', 'content': 'Changed'}])
        result = await app.run('director', [], {'calls': 16}, {}, self.trace(), 'root', mutation_requested=True)
        recovery = app.request.call_args_list[1]
        self.assertFalse(recovery.kwargs['thinking_enabled'])
        self.assertFalse(recovery.args[8])  # force_final
        self.assertIn('apply_patch', [t['function']['name'] for t in recovery.args[2]])
        self.assertEqual(result['status'], 'partial')  # configured check was not run

    async def test_timeout_recovers_with_editing_tools(self):
        app = self.app()
        app.request = AsyncMock(side_effect=[TimeoutError(),
            self.response('apply_patch', {'patch': 'small patch'}),
            {'role': 'assistant', 'content': 'Changed'}])
        await app.run('director', [], {'calls': 16}, {}, self.trace(), 'root', mutation_requested=True)
        self.assertFalse(app.request.call_args_list[1].kwargs['thinking_enabled'])
        self.assertFalse(app.request.call_args_list[1].args[8])

    async def test_prose_is_not_accepted_instead_of_implementation(self):
        app = self.app()
        app.request = AsyncMock(side_effect=[
            {'role': 'assistant', 'content': 'No changes needed: dispatcher is closed.'},
            self.response('report_blocker', {'reason': 'missing_information', 'evidence': 'The requested endpoint contract is missing.', 'next_step': 'Provide the contract.'}),
            {'role': 'assistant', 'content': 'Blocked; no files changed.'}])
        trace = self.trace()
        result = await app.run('director', [], {'calls': 16}, {}, trace, 'root', mutation_requested=True)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(any(c.args[0] == 'implementation_answer_rejected' for c in trace.event.call_args_list))
        self.assertTrue(any(c.args[0] == 'implementation_blocked' for c in trace.event.call_args_list))
        app.repository.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
