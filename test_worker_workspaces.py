import asyncio
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from git_tools import RepositoryTools
from orchestra import TaskScheduler
from runtime_settings import load_runtime_settings
from tool_errors import ToolError
from worker_workspaces import WorkerWorkspaces
import test_base_tools


class WorkspaceTransfer(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='orchestra-workspaces-')
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.root = self.directory / 'project'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root / 'a.txt').write_text('base\n')
        self.git('add', 'a.txt')
        self.git('commit', '-qm', 'baseline')
        self.repo = RepositoryTools(self.root)
        self.manager = WorkerWorkspaces(self.repo, self.directory / 'workspaces')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_snapshot_includes_dirty_and_staged_files_without_touching_director(self):
        (self.root / 'a.txt').write_text('staged\n')
        self.git('add', 'a.txt')
        (self.root / 'a.txt').write_text('current dirty\n')
        (self.root / 'skeleton.py').write_text('value = 1\n')
        head = self.git('rev-parse', 'HEAD')
        index = self.git('diff', '--cached', '--binary')
        worker = self.manager.create('task_dirty')
        self.assertEqual((worker.root / 'a.txt').read_text(), 'current dirty\n')
        self.assertEqual((worker.root / 'skeleton.py').read_text(), 'value = 1\n')
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.git('diff', '--cached', '--binary'), index)
        worker.create_file('new.py', 'value = 2\n')
        self.manager.collect('task_dirty')
        review = self.manager.review('task_dirty')
        self.assertEqual(review['changed_paths'], ['new.py'])
        self.manager.apply('task_dirty', review['fingerprint'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'current dirty\n')
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 2\n')
        self.assertEqual(self.git('diff', '--cached', '--binary'), index)

    def test_two_workers_are_isolated_and_conflicting_alternative_is_not_applied(self):
        first = self.manager.create('task_a')
        second = self.manager.create('task_b')
        first.replace_text('a.txt', 'base', 'first')
        second.replace_text('a.txt', 'base', 'second')
        self.assertEqual((self.root / 'a.txt').read_text(), 'base\n')
        self.manager.collect('task_a'); self.manager.collect('task_b')
        self.manager.apply('task_a', self.manager.review('task_a')['fingerprint'])
        with self.assertRaises(ToolError) as error:
            self.manager.apply('task_b', self.manager.review('task_b')['fingerprint'])
        self.assertEqual(error.exception.code, 'worker_changes_conflict')
        self.assertEqual((self.root / 'a.txt').read_text(), 'first\n')
        self.assertEqual((second.root / 'a.txt').read_text(), 'second\n')

    def test_disjoint_workers_can_both_be_applied_and_transfer_is_not_repeated(self):
        for task, path in [('task_a', 'one.py'), ('task_b', 'two.py')]:
            worker = self.manager.create(task)
            worker.create_file(path, 'value = 2\n')
            self.manager.collect(task)
        for task in ('task_a', 'task_b'):
            self.manager.apply(task, self.manager.review(task)['fingerprint'])
        self.assertTrue((self.root / 'one.py').exists() and (self.root / 'two.py').exists())
        with self.assertRaises(ToolError) as error:
            self.manager.apply('task_a', self.manager.review('task_a')['fingerprint'])
        self.assertEqual(error.exception.code, 'worker_changes_already_applied')

    def test_conflict_rejects_entire_patch_including_new_files(self):
        worker = self.manager.create('task_a')
        worker.replace_text('a.txt', 'base', 'worker')
        worker.create_file('new.py', 'value = 2')
        self.manager.collect('task_a')
        (self.root / 'a.txt').write_text('director\n')
        with self.assertRaises(ToolError):
            self.manager.apply('task_a', self.manager.review('task_a')['fingerprint'])
        self.assertFalse((self.root / 'new.py').exists())
        self.assertEqual((self.root / 'a.txt').read_text(), 'director\n')

    def test_empty_results_and_incorrect_fingerprint_are_explicit(self):
        worker = self.manager.create('task_a')
        with self.assertRaises(ToolError):
            self.manager.review('task_a')
        self.manager.collect('task_a')
        with self.assertRaises(ToolError) as error:
            self.manager.apply('task_a', self.manager.review('task_a')['fingerprint'])
        self.assertEqual(error.exception.code, 'no_worker_changes')
        worker.create_file('new.py', 'value = 2')
        self.manager.collect('task_a')
        with self.assertRaises(ToolError) as error:
            self.manager.apply('task_a', 'wrong')
        self.assertEqual(error.exception.code, 'stale_worker_review')

    def test_unborn_repository_refuses_implementation_worker(self):
        unborn = self.directory / 'unborn'
        unborn.mkdir()
        subprocess.run(['git', '-C', str(unborn), 'init', '-q'], check=True)
        manager = WorkerWorkspaces(RepositoryTools(unborn), self.directory / 'unused')
        with self.assertRaises(ToolError) as error:
            manager.create('task_a')
        self.assertEqual(error.exception.code, 'worker_needs_commit')
        self.assertFalse(manager.directory.exists())

    def test_unsupported_deletion_is_retained_for_review_but_not_applied(self):
        worker = self.manager.create('task_a')
        (worker.root / 'a.txt').unlink()
        result = self.manager.collect('task_a')
        self.assertFalse(result['transfer_supported'])
        self.manager.review('task_a')
        with self.assertRaises(ToolError) as error:
            self.manager.apply('task_a', result['fingerprint'])
        self.assertEqual(error.exception.code, 'unsupported_worker_patch')
        self.assertTrue((self.root / 'a.txt').exists())

    def test_transfer_requires_review_and_per_file_review_is_frozen(self):
        worker = self.manager.create('task_a')
        worker.create_file('new.py', 'value = 2\n')
        result = self.manager.collect('task_a')
        with self.assertRaises(ToolError) as error:
            self.manager.apply('task_a', result['fingerprint'])
        self.assertEqual(error.exception.code, 'worker_review_required')
        (worker.root / 'new.py').write_text('value = 999\n')
        worker._run(['add', 'new.py'])
        reviewed = self.manager.review('task_a', 'new.py')
        self.assertIn('+value = 2', reviewed['diff'])
        self.assertNotIn('999', reviewed['diff'])
        self.manager.apply('task_a', reviewed['fingerprint'])
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 2\n')

    def test_checkout_line_ending_normalization_does_not_create_false_conflict(self):
        self.git('config', 'core.autocrlf', 'true')
        self.git('config', 'core.safecrlf', 'false')
        worker = self.manager.create('task_crlf')
        worker.replace_text('a.txt', 'base', 'worker')
        self.manager.collect('task_crlf')
        review = self.manager.review('task_crlf')
        self.manager.apply('task_crlf', review['fingerprint'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'worker\n')

    def test_collection_excludes_changes_removed_by_git_normalization(self):
        self.git('config', 'core.autocrlf', 'true')
        self.git('config', 'core.safecrlf', 'false')
        worker = self.manager.create('task_normalized')
        (worker.root / 'a.txt').write_bytes(b'base\n')
        worker.create_file('new.py', 'value = 2\n')
        collected = self.manager.collect('task_normalized')
        self.assertEqual(collected['changed_paths'], ['new.py'])
        self.assertNotIn('a/a.txt', self.manager.review('task_normalized')['diff'])

    def test_line_ending_only_changes_produce_empty_worker_patch(self):
        self.git('config', 'core.autocrlf', 'true')
        self.git('config', 'core.safecrlf', 'false')
        worker = self.manager.create('task_no_change')
        (worker.root / 'a.txt').write_bytes(b'base\n')
        collected = self.manager.collect('task_no_change')
        self.assertEqual(collected['changed_paths'], [])
        self.assertEqual(collected['patch_characters'], 0)


class EditingWorkerRuntime(unittest.IsolatedAsyncioTestCase):
    setUp = test_base_tools.RuntimeContracts.setUp
    call = test_base_tools.RuntimeContracts.call
    response = test_base_tools.RuntimeContracts.response

    def prepare(self):
        self.app.settings = load_runtime_settings('settings.json', 'project')
        self.app.repository._run(['init', '-q'])
        repo = RepositoryTools(self.root, checks=self.app.repository.checks)
        repo._run(['config', 'user.name', 'Test'])
        repo._run(['config', 'user.email', 'test@example.invalid'])
        (self.root / 'baseline.txt').write_text('baseline\n')
        repo._run(['add', 'baseline.txt']); repo._run(['commit', '-qm', 'baseline'])
        self.app.repository = repo
        return TaskScheduler(self.app, {'calls': 20}, {}, self.trace)

    async def test_worker_edits_checks_and_director_reviews_applies_and_rechecks(self):
        scheduler = self.prepare()
        counts = {'director': 0, 'worker': 0}

        async def respond(*args, **kwargs):
            role = args[0]
            index = counts[role]; counts[role] += 1
            if role == 'worker':
                names = {t['function']['name'] for t in args[2]}
                self.assertTrue({'create_file', 'replace_text', 'run_check'} <= names)
                checker = next(t for t in args[2] if t['function']['name'] == 'run_check')
                self.assertEqual(checker['function']['parameters']['properties']['name']['enum'], ['unit'])
                self.assertIn('configured checks=["unit"]', args[3])
                self.assertNotIn('create_commit', names)
                self.assertNotIn('delegate_task', names)
                return [self.response('create_file', {'path': 'new.py', 'content': 'value = 2\n'}),
                        {'role': 'assistant', 'content': 'Implemented new.py; unit passed.'}][index]
            if index == 0:
                return self.response('delegate_task', {'task': 'Create new.py with value = 2 and test it.',
                    'max_calls': 4, 'report_target_tokens': 500, 'mode': 'implementation'})
            if index == 1:
                return self.response('wait_tasks', {})
            task_id = next(iter(scheduler.tasks))
            if index == 2:
                self.assertFalse((self.root / 'new.py').exists())
                self.assertNotIn('new.py', self.trace.modified_files)
                return self.response('review_task_changes', {'task_id': task_id})
            if index == 3:
                review = scheduler.workspaces.review(task_id)
                return self.response('apply_task_changes', {'task_id': task_id, 'fingerprint': review['fingerprint']})
            if index == 4:
                return self.response('run_check', {'name': 'unit'})
            return {'role': 'assistant', 'content': 'Transferred and checked.'}

        self.app.request = AsyncMock(side_effect=respond)
        result = await self.app.run('director', [], scheduler.shared_budget, {}, self.trace,
            self.task, scheduler=scheduler, max_delegations=2, mutation_requested=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 2\n')
        self.assertEqual(self.trace.modified_files, {'new.py'})
        events = [e for e in self.trace.events if e['event'] == 'tool_finished']
        self.assertTrue(all(e['ok'] for e in events))
        self.assertEqual(len([e for e in events if e['tool'] == 'run_check']), 2)

    async def test_partial_worker_changes_survive_cancellation(self):
        scheduler = self.prepare()
        wrote = asyncio.Event()

        async def respond(*args, **kwargs):
            if not wrote.is_set():
                wrote.set()
                return self.response('create_file', {'path': 'partial.py', 'content': 'value = 1'})
            await asyncio.Event().wait()

        self.app.request = AsyncMock(side_effect=respond)
        submitted = scheduler.submit('worker-1', 'Implement a feature.', self.task, 4, 500, mode='implementation')
        while not any(e['event'] == 'tool_finished' and e['tool'] == 'create_file' for e in self.trace.events):
            await asyncio.sleep(0.01)
        await scheduler.collect_for_finalization(cancel_reason='test_deadline')
        record = scheduler.tasks[submitted['task_id']]
        self.assertEqual(record['status'], 'cancelled')
        self.assertIn('partial.py', record['changes']['changed_paths'])
        self.assertEqual(record['result_status'], 'partial')
        self.assertEqual(record['result_source'], 'workspace')
        artifact = json.loads(Path(record['changes']['artifact_file']).read_text())
        self.assertIn('partial.py', artifact['paths'])
        self.assertFalse((self.root / 'partial.py').exists())

    async def test_disabled_editing_is_rejected_before_task_allocation(self):
        scheduler = self.prepare()
        self.app.settings['roles']['worker']['allow_editing'] = False
        before = scheduler.shared_budget['calls']
        with self.assertRaises(ToolError):
            scheduler.submit('worker-1', 'Implement.', self.task, 4, 500, mode='implementation')
        self.assertEqual(scheduler.shared_budget['calls'], before)
        self.assertEqual(scheduler.tasks, {})

    async def test_two_implementation_workers_run_concurrently_in_distinct_roots(self):
        scheduler = self.prepare()
        started, ready, counts = set(), asyncio.Event(), {}

        async def respond(*args, **kwargs):
            label = args[7]
            index = counts.get(label, 0); counts[label] = index + 1
            if index == 0:
                started.add(label)
                if len(started) == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)
                value = 1 if label == 'worker-1' else 2
                return self.response('create_file', {'path': 'new.py', 'content': f'value = {value}\n'})
            return {'role': 'assistant', 'content': 'Private implementation and check recorded.'}

        self.app.request = AsyncMock(side_effect=respond)
        one = scheduler.submit('worker-1', 'Implement alternative A.', self.task, 3, 500, mode='implementation')
        two = scheduler.submit('worker-2', 'Implement alternative B.', self.task, 3, 500, mode='implementation')
        result = await scheduler.wait(timeout_seconds=20)
        self.assertTrue(result['all_finished'])
        self.assertEqual(started, {'worker-1', 'worker-2'})
        first = Path(scheduler.tasks[one['task_id']]['changes']['workspace'])
        second = Path(scheduler.tasks[two['task_id']]['changes']['workspace'])
        self.assertNotEqual(first, second)
        self.assertEqual((first / 'new.py').read_text(), 'value = 1\n')
        self.assertEqual((second / 'new.py').read_text(), 'value = 2\n')
        self.assertFalse((self.root / 'new.py').exists())

    async def test_apply_mixed_with_edit_performs_neither(self):
        scheduler = self.prepare()
        scheduler.workspaces = WorkerWorkspaces(self.app.repository, self.trace.directory / 'workspaces')
        worker = scheduler.workspaces.create('task_offline')
        worker.create_file('new.py', 'value = 2')
        change = scheduler.workspaces.collect('task_offline')
        self.app.request = AsyncMock(side_effect=[
            {'role': 'assistant', 'content': None, 'tool_calls': [
                self.call('apply_task_changes', {'task_id': 'task_offline', 'fingerprint': change['fingerprint']}),
                self.call('create_file', {'path': 'other.py', 'content': 'value = 1'})]},
            self.response('report_blocker', {'reason': 'missing_information', 'evidence': 'Need integration choice.', 'next_step': 'Review alternatives.'}),
            {'role': 'assistant', 'content': 'Not applied.'}])
        messages = []
        await self.app.run('director', messages, scheduler.shared_budget, {}, self.trace,
            self.task, scheduler=scheduler, mutation_requested=True)
        self.assertFalse((self.root / 'new.py').exists())
        self.assertFalse((self.root / 'other.py').exists())
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        self.assertEqual([o['error'] for o in outputs[:2]], ['conflicting_actions', 'conflicting_actions'])


class CandidatePromotion(unittest.TestCase):
    setUp = WorkspaceTransfer.setUp
    git = WorkspaceTransfer.git

    def candidate(self):
        checkpoint = self.manager.clean_checkpoint()
        worker = self.manager.create('task_choice')
        worker.create_file('new.py', 'value = 2\n')
        self.manager.collect('task_choice')
        review = self.manager.review('task_choice')
        return worker, checkpoint, review['fingerprint']

    def test_successful_promotion_checks_commits_and_becomes_next_baseline(self):
        self.repo.checks = {'unit': [sys.executable, '-B', '-c', 'from new import value; assert value == 2']}
        _, checkpoint, fingerprint = self.candidate()
        result = self.manager.promote('task_choice', fingerprint, 'feat: accept implementation', checkpoint)
        self.assertTrue(result['promoted'])
        self.assertTrue(result['checks'][0]['passed'])
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        next_worker = self.manager.create('task_next')
        self.assertEqual((next_worker.root / 'new.py').read_text(), 'value = 2\n')
        self.assertEqual(next_worker._run(['rev-parse', 'HEAD'])['stdout'].strip(), result['commit'])

    def test_failed_check_rolls_back_only_candidate_and_preserves_worktree(self):
        self.repo.checks = {'unit': [sys.executable, '-B', '-c', 'assert False']}
        worker, checkpoint, fingerprint = self.candidate()
        result = self.manager.promote('task_choice', fingerprint, 'feat: reject implementation', checkpoint)
        self.assertFalse(result['promoted'])
        self.assertTrue(result['rolled_back'])
        self.assertEqual(result['error'], 'candidate_check_failed')
        self.assertFalse((self.root / 'new.py').exists())
        self.assertTrue((worker.root / 'new.py').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD'), checkpoint['head'])
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_commit_failure_also_restores_the_accepted_baseline(self):
        _, checkpoint, fingerprint = self.candidate()
        with patch.object(self.repo, 'create_commit', side_effect=ToolError('git_identity_missing', 'Configure Git identity.')):
            result = self.manager.promote('task_choice', fingerprint, 'feat: accept', checkpoint)
        self.assertFalse(result['promoted'])
        self.assertTrue(result['rolled_back'])
        self.assertFalse((self.root / 'new.py').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD'), checkpoint['head'])

    def test_changed_branch_or_dirty_workspace_is_rejected_before_transfer(self):
        _, checkpoint, fingerprint = self.candidate()
        self.git('switch', '-c', 'another')
        with self.assertRaises(ToolError) as error:
            self.manager.promote('task_choice', fingerprint, 'feat: accept', checkpoint)
        self.assertEqual(error.exception.code, 'comparison_baseline_changed')
        self.assertFalse((self.root / 'new.py').exists())
        (self.root / 'user.txt').write_text('keep')
        with self.assertRaises(ToolError):
            self.manager.promote('task_choice', fingerprint, 'feat: accept', checkpoint)
        self.assertEqual((self.root / 'user.txt').read_text(), 'keep')

    def test_check_that_changes_source_is_not_overwritten_by_rollback(self):
        self.repo.checks = {'unit': [sys.executable, '-B', '-c',
            "from pathlib import Path; Path('new.py').write_text('value = 999\\n')"]}
        _, checkpoint, fingerprint = self.candidate()
        result = self.manager.promote('task_choice', fingerprint, 'feat: accept', checkpoint)
        self.assertFalse(result['promoted'])
        self.assertFalse(result['rolled_back'])
        self.assertTrue(result['workspace_changed'])
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 999\n')
        self.assertEqual(self.git('rev-parse', 'HEAD'), checkpoint['head'])

    def test_extra_check_side_effect_is_disclosed_after_owned_patch_rollback(self):
        self.repo.checks = {'unit': [sys.executable, '-B', '-c',
            "from pathlib import Path; Path('extra.txt').write_text('check effect')"]}
        _, checkpoint, fingerprint = self.candidate()
        result = self.manager.promote('task_choice', fingerprint, 'feat: accept', checkpoint)
        self.assertFalse(result['promoted'])
        self.assertFalse(result['rolled_back'])
        self.assertEqual(result['files'], ['extra.txt'])
        self.assertFalse((self.root / 'new.py').exists())
        self.assertEqual((self.root / 'extra.txt').read_text(), 'check effect')

    def test_expired_promotion_deadline_restores_the_checkpoint(self):
        _, checkpoint, fingerprint = self.candidate()
        result = self.manager.promote('task_choice', fingerprint, 'feat: accept', checkpoint, deadline=0)
        self.assertFalse(result['promoted'])
        self.assertTrue(result['rolled_back'])
        self.assertEqual(result['error'], 'promotion_timeout')
        self.assertFalse((self.root / 'new.py').exists())


class ComparisonRuntime(unittest.IsolatedAsyncioTestCase):
    setUp = test_base_tools.RuntimeContracts.setUp
    call = test_base_tools.RuntimeContracts.call
    response = test_base_tools.RuntimeContracts.response
    prepare = EditingWorkerRuntime.prepare

    async def test_reserved_pair_finishes_before_director_reviews_and_promotes(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        scheduler.shared_budget['calls'] = 13
        released = asyncio.Event()
        original_collect = scheduler.collect_for_finalization
        deadline = time.monotonic() + 90

        async def collect(**kwargs):
            self.assertEqual(kwargs['wait_until'], deadline)
            self.assertTrue(kwargs['wait_for_workers'])
            self.assertGreater(deadline - time.monotonic(), 15)
            released.set()
            return await original_collect(**kwargs)

        scheduler.collect_for_finalization = AsyncMock(side_effect=collect)
        counts = {}

        async def respond(*args, **kwargs):
            role, label = args[0], args[7]
            index = counts.get(label, 0)
            counts[label] = index + 1
            if role == 'worker':
                await released.wait()
                if index == 0:
                    return self.response('create_file', {'path': 'new.py', 'content': 'value = 2\n'})
                return {'role': 'assistant', 'content': 'Implemented.'}
            if index == 0:
                return self.response('start_comparison_round', {
                    'task': 'Implement one value.', 'max_calls_per_worker': 3,
                    'report_target_tokens': 500})
            current = scheduler.rounds[scheduler.active_round_id] if scheduler.active_round_id else None
            if index in (1, 2):
                self.assertTrue(all(r['status'] != 'running' for r in scheduler.tasks.values()))
                return self.response('review_task_changes', {'task_id': current['task_ids'][index - 1]})
            if index == 3:
                chosen = current['task_ids'][0]
                return self.response('promote_candidate', {
                    'round_id': current['round_id'], 'task_id': chosen,
                    'fingerprint': scheduler.workspaces.review(chosen)['fingerprint'],
                    'message': 'feat: accept value', 'rationale': 'Verified implementation.'})
            return {'role': 'assistant', 'content': 'Milestone accepted.'}

        self.app.request = AsyncMock(side_effect=respond)
        result = await self.app.run('director', [], scheduler.shared_budget, {}, self.trace,
            self.task, scheduler=scheduler, max_delegations=2, mutation_requested=True,
            finalization_deadline=deadline, turn_deadline=deadline + 30)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        scheduler.collect_for_finalization.assert_awaited_once()
        self.assertEqual(counts['director'], 5)
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 2\n')
        self.assertTrue(all(r['status'] != 'cancelled' for r in scheduler.tasks.values()))

    async def test_real_deadline_still_cancels_reserved_worker(self):
        scheduler = self.prepare()
        started = asyncio.Event()

        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.app.run = AsyncMock(side_effect=blocked)
        scheduler.submit('worker-1', 'Wait for deadline.', self.task, 3, 500)
        await started.wait()
        result = await scheduler.collect_for_finalization(
            wait_until=time.monotonic(), wait_for_workers=True)
        self.assertEqual(result[0]['status'], 'cancelled')
        self.assertEqual(result[0]['stop_reason'], 'finalization_deadline')

    async def test_two_rounds_choose_whole_candidates_and_rebase_next_pair(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        scheduler.shared_budget['calls'] = 48
        self.app.repository.checks = {'unit': [sys.executable, '-B', '-c', 'from new import value; assert value >= 2']}
        counts = {}
        observed_bases = {}

        async def respond(*args, **kwargs):
            role, label = args[0], args[7]
            index = counts.get(label, 0); counts[label] = index + 1
            if role == 'worker':
                record = next(record for record in scheduler.tasks.values() if record['agent'] == label)
                repo = scheduler.workspaces.records[record['task_id']]['repository']
                if index == 0:
                    observed_bases[label] = repo._run(['rev-parse', 'HEAD'])['stdout'].strip()
                    number = int(label.split('-')[-1])
                    if number <= 2:
                        return self.response('create_file', {'path': 'new.py', 'content': f'value = {number}\n'})
                    self.assertEqual((repo.root / 'new.py').read_text(), 'value = 2\n')
                    return self.response('replace_text', {'path': 'new.py', 'old_text': 'value = 2', 'new_text': f'value = {number}'})
                return {'role': 'assistant', 'content': 'Candidate implemented; actual check result recorded.'}
            names = {tool['function']['name'] for tool in args[2]}
            self.assertFalse({'create_file', 'replace_text', 'apply_patch', 'apply_task_changes', 'create_commit'} & names)
            if index in (0, 5):
                return self.response('start_comparison_round', {'task': f'Implement milestone {index // 5 + 1}.',
                    'max_calls_per_worker': 3, 'report_target_tokens': 500})
            if index in (1, 6):
                return self.response('wait_tasks', {})
            current = scheduler.rounds[scheduler.active_round_id] if scheduler.active_round_id else None
            if index in (2, 3, 7, 8):
                offset = 0 if index in (2, 7) else 1
                return self.response('review_task_changes', {'task_id': current['task_ids'][offset]})
            if index in (4, 9):
                choice = current['task_ids'][1 if index == 4 else 0]
                return self.response('promote_candidate', {'round_id': current['round_id'], 'task_id': choice,
                    'fingerprint': scheduler.workspaces.review(choice)['fingerprint'],
                    'message': f'feat: accept milestone {index // 5 + 1}', 'rationale': 'Chosen complete candidate meets this milestone.'})
            return {'role': 'assistant', 'content': 'Two milestones accepted and committed.'}

        self.app.request = AsyncMock(side_effect=respond)
        result = await self.app.run('director', [], scheduler.shared_budget, {}, self.trace, self.task,
            scheduler=scheduler, max_delegations=2, mutation_requested=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual((self.root / 'new.py').read_text(), 'value = 3\n')
        rounds = list(scheduler.rounds.values())
        self.assertEqual([record['status'] for record in rounds], ['promoted', 'promoted'])
        self.assertEqual(observed_bases['worker-1'], observed_bases['worker-2'])
        self.assertEqual(observed_bases['worker-3'], observed_bases['worker-4'])
        self.assertEqual(observed_bases['worker-3'], rounds[0]['commit'])
        self.assertEqual(self.app.repository._run(['rev-list', '--count', 'HEAD'])['stdout'].strip(), '3')
        for record in rounds:
            tasks = [scheduler.tasks[key] for key in record['task_ids']]
            descriptions = [self.trace.tasks[item['task_id']]['description'] for item in tasks]
            self.assertEqual(descriptions[0], descriptions[1])

    async def test_director_forged_edit_is_rejected_in_comparison_mode(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        self.app.request = AsyncMock(side_effect=[
            self.response('create_file', {'path': 'forbidden.py', 'content': 'value = 1'}),
            self.response('report_blocker', {'reason': 'missing_information', 'evidence': 'No accepted candidate.', 'next_step': 'Start a paired round.'}),
            {'role': 'assistant', 'content': 'No implementation accepted.'}])
        messages = []
        result = await self.app.run('director', messages, scheduler.shared_budget, {}, self.trace, self.task,
                                   scheduler=scheduler, mutation_requested=True)
        self.assertFalse((self.root / 'forbidden.py').exists())
        self.assertEqual(result['status'], 'partial')
        error = json.loads(next(m['content'] for m in messages if m['role'] == 'tool'))
        self.assertEqual(error['error'], 'tool_unavailable')

    async def test_round_rejection_preserves_baseline_and_round_limit_is_enforced(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        self.app.settings['execution']['max_comparison_rounds'] = 1
        self.app.run = AsyncMock(return_value={'answer': 'No changes proposed.', 'status': 'partial', 'result_source': 'final'})
        started = await scheduler.start_comparison('Inspect implementation options.', 2, 500, self.task)
        await scheduler.wait(timeout_seconds=20)
        before = self.app.repository._run(['rev-parse', 'HEAD'])['stdout']
        result = scheduler.reject_comparison(started['round_id'], 'Neither implements the requirement.')
        self.assertEqual(result['status'], 'rejected')
        self.assertIsNone(scheduler.active_round_id)
        self.assertEqual(self.app.repository._run(['rev-parse', 'HEAD'])['stdout'], before)
        with self.assertRaises(ToolError) as error:
            await scheduler.start_comparison('Try again.', 2, 500, self.task)
        self.assertEqual(error.exception.code, 'comparison_round_limit')

    async def test_pair_budget_reserves_director_calls_before_starting_workers(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        before = scheduler.shared_budget['calls']
        with self.assertRaises(ToolError) as error:
            await scheduler.start_comparison('Implement.', 12, 500, self.task)
        self.assertEqual(error.exception.code, 'batch_worker_budget_exceeded')
        self.assertEqual(scheduler.shared_budget['calls'], before)
        self.assertEqual(scheduler.tasks, {})

    async def test_both_available_alternatives_must_be_reviewed(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        self.app.run = AsyncMock(return_value={'answer': 'No changes.', 'status': 'partial', 'result_source': 'final'})
        started = await scheduler.start_comparison('Implement.', 2, 500, self.task)
        await scheduler.wait(timeout_seconds=20)
        chosen = started['workers'][0]['task_id']
        review = scheduler.workspaces.review(chosen)
        with self.assertRaises(ToolError) as error:
            scheduler.promote_comparison(started['round_id'], chosen, review['fingerprint'], 'feat: accept', 'Prefer A.')
        self.assertEqual(error.exception.code, 'worker_review_required')

    async def test_round_start_mixed_with_other_tools_starts_no_workers(self):
        scheduler = self.prepare()
        self.app.settings = load_runtime_settings('settings.json', 'comparison')
        self.app.request = AsyncMock(side_effect=[
            {'role': 'assistant', 'content': None, 'tool_calls': [
                self.call('start_comparison_round', {'task': 'Implement.', 'max_calls_per_worker': 2, 'report_target_tokens': 500}),
                self.call('read_file', {'path': 'baseline.txt'})]},
            self.response('report_blocker', {'reason': 'missing_information', 'evidence': 'No round started.', 'next_step': 'Start the round separately.'}),
            {'role': 'assistant', 'content': 'No round started.'}])
        messages = []
        await self.app.run('director', messages, scheduler.shared_budget, {}, self.trace, self.task,
                           scheduler=scheduler, mutation_requested=True)
        self.assertEqual(scheduler.tasks, {})
        self.assertIsNone(scheduler.workspaces)
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        self.assertEqual([o['error'] for o in outputs[:2]], ['conflicting_actions', 'conflicting_actions'])
