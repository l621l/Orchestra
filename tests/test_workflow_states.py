import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from context_compaction import compact_tool_history, context_breakdown
from tool_errors import model_result
from tests import test_single_model as fixtures
from tests import test_context_compaction as compact_fixtures


class States(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SingleModel.setUp

    def outcome(self, state, blocker=None):
        return {'answer':json.dumps({'summary':'progress','next_steps':'next','done':state in
            {'ready_for_confirmation','complete'},'state':state,'blocker':blocker}),
            'status':'completed','modified_files':[], 'verification':{'status':'passed'}}

    async def test_needs_user_stops_with_evidence(self):
        blocker={'action':'Install pygame','reason':'No installation tool is available',
                 'evidence':'ModuleNotFoundError: pygame during configured check'}
        self.app.run=AsyncMock(return_value=self.outcome('needs_user',blocker))
        result=await self.app.run_iterations('original',{},self.trace,self.task)
        self.assertEqual(self.app.run.call_count,1)
        self.assertEqual(result['chain_state'],'needs_user')
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['blocker'],blocker)
        self.assertIn(blocker['action'],result['answer'])

    async def test_missing_evidence_does_not_stop_chain(self):
        self.settings['execution']['iterations_per_task']=2
        self.app.run=AsyncMock(return_value=self.outcome('needs_user'))
        result=await self.app.run_iterations('original',{},self.trace,self.task)
        self.assertEqual(self.app.run.call_count,2)
        self.assertEqual(result['chain_state'],'continue')
        self.assertEqual(result['status'],'partial')

    async def test_explicit_states_follow_confirmation(self):
        self.app.run=AsyncMock(side_effect=[self.outcome('ready_for_confirmation'),self.outcome('complete')])
        result=await self.app.run_iterations('original',{},self.trace,self.task)
        self.assertEqual(result['chain_state'],'complete')
        self.assertTrue(result['completion_confirmed'])


    async def test_disabled_mimo_omits_reasoning_without_mutating_history(self):
        from types import SimpleNamespace
        self.app.config['roles']['director']['model']='mimo-v2.6-pro'
        chunk=SimpleNamespace(id='offline',model='mimo-v2.6-pro',usage=None,
            choices=[SimpleNamespace(finish_reason='stop',delta=SimpleNamespace(
                content='Report',reasoning_content=None,tool_calls=None))])
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            def __aiter__(self):
                async def generate(): yield chunk
                return generate()
        create=AsyncMock(return_value=Stream())
        self.app.clients['offline']=SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        history=[{'role':'assistant','content':'previous','reasoning_content':'old thought'}]
        await self.app.request('director',history,[],'',{},self.trace,self.task,
                               thinking_override='disabled')
        self.assertNotIn('reasoning_content',create.call_args.kwargs['messages'][0])
        self.assertEqual(history[0]['reasoning_content'],'old thought')
        event=next(e for e in self.trace.events if e['event']=='context_composition')
        self.assertEqual(event['reasoning_characters_omitted'],11)
        self.assertEqual(event['reasoning'],0)


class ContextEconomy(unittest.TestCase):
    pair = compact_fixtures.Compaction.pair

    def test_verified_edits_archive_before_size_threshold(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[*self.pair('edit'),*self.pair('check','run_check',{'name':'unit'},
                {'passed':True,'output':'Ran 1 test OK'})]
            stats=compact_tool_history(messages,Path(folder)/'a.json',threshold=100000,verified=True)
            self.assertIsNotNone(stats)
            self.assertNotIn('tool_calls',messages[0])
            self.assertIn('execution_record',json.loads(messages[0]['content']))
            self.assertTrue(json.loads(messages[-1]['content'])['result']['passed'])

    def test_composition_separates_payloads(self):
        messages=[{'role':'system','content':'rules'},*self.pair('code'),
            *self.pair('read','read_file',{'path':'a.py'},{'content':'source'}),
            {'role':'assistant','content':'report','reasoning_content':'thought'}]
        sizes=context_breakdown(messages,[{'tool':'schema'}])
        self.assertGreater(sizes['edit_arguments'],8000)
        self.assertGreater(sizes['file_reads'],0)
        self.assertEqual(sizes['reasoning'],7)
        self.assertGreater(sizes['tool_schemas'],0)

    def test_repeated_read_compacts_below_threshold(self):
        with tempfile.TemporaryDirectory() as folder:
            messages=[*self.pair('read1','read_file',{'path':'a.py'},{'content':'x'*8000}),
                      *self.pair('middle'),
                      *self.pair('read2','read_file',{'path':'a.py'},{'content':'x'*8000})]
            stats=compact_tool_history(messages,Path(folder)/'a.json',threshold=100000)
            self.assertIsNotNone(stats)
            self.assertNotIn('tool_calls',messages[0])
            self.assertNotIn('content',json.loads(messages[0]['content'])['execution_record'][0]['result'])
            self.assertEqual(json.loads(messages[-1]['content'])['result']['content'],'x'*8000)

    def test_success_output_keeps_totals_and_original(self):
        full='\n'.join(f'test_{i} (Tests) ... ok' for i in range(100))+'\nRan 100 tests\nOK'
        original={'passed':True,'returncode':0,'output':full}
        result=model_result('run_check',original)
        self.assertEqual(original['output'],full)
        self.assertNotIn('test_99',result['output'])
        self.assertIn('Ran 100 tests',result['output'])
        self.assertLess(len(result['output']),100)

    def test_failure_keeps_traceback_and_warnings(self):
        full='test_ok (Tests) ... ok\ntest_bad (Tests) ... FAIL\nResourceWarning: open file\nTraceback\nAssertionError: wrong\nFAILED (failures=1)'
        result=model_result('run_check',{'passed':False,'output':full})
        self.assertNotIn('test_ok',result['output'])
        self.assertIn('AssertionError: wrong',result['output'])
        self.assertIn('ResourceWarning',result['output'])
        self.assertIn('FAILED',result['output'])
