"""Isolated worker checkouts and explicit, conflict-checked patch transfer."""
import hashlib
import json
import shutil
import time
from pathlib import Path

from git_tools import RepositoryTools
from tool_errors import ToolError


def file_hash(path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ToolError('unsupported_worker_file', 'Worker changes must use regular files.')
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


class WorkerWorkspaces:
    def __init__(self, repository, directory):
        self.parent = repository
        self.directory = Path(directory).resolve()
        self.records = {}

    def clean_checkpoint(self):
        self.parent._require_repository()
        head = self.parent._run(['rev-parse', '--verify', 'HEAD'], check=False)
        if head['returncode']:
            raise ToolError('comparison_needs_commit', 'Comparison rounds require an initial commit. Commit the specification before starting.')
        if self.parent._changed_paths(staged=True):
            raise ToolError('comparison_requires_clean_workspace', 'Resolve staged changes before starting or promoting a comparison round.')
        try:
            self.parent.prepare_commit()
        except ToolError as error:
            if error.code != 'nothing_to_commit':
                raise
        else:
            raise ToolError('comparison_requires_clean_workspace', 'The accepted workspace must be clean. Preserve and commit or resolve existing changes before comparison.')
        branch = self.parent._run(['symbolic-ref', '--quiet', 'HEAD'], check=False)['stdout'].strip()
        if not branch:
            raise ToolError('comparison_needs_branch', 'Select a working branch before starting a comparison round.')
        return {'head': head['stdout'].strip(), 'branch': branch}

    def promote(self, task_id, fingerprint, message, checkpoint, deadline=None):
        if self.clean_checkpoint() != checkpoint:
            raise ToolError('comparison_baseline_changed', 'The director branch or HEAD changed during the round; do not promote an obsolete alternative.')
        checks, applied, post_apply = [], False, {}
        record = self.records[task_id]
        artifact = record['artifact']
        # Precondition failures must not be confused with a failed verification.
        result = self.apply(task_id, fingerprint)
        applied = True
        try:
            post_apply = {path: file_hash(self.parent.policy.resolve_write(path)) for path in artifact['paths']}
            for name in sorted(self.parent.checks):
                timeout = self.parent.CHECK_TIMEOUT
                if deadline is not None:
                    timeout = min(timeout, deadline - time.monotonic())
                    if timeout <= 0:
                        raise ToolError('promotion_timeout', 'Not enough time remains to verify and commit this candidate.')
                started = time.monotonic()
                try:
                    checked = self.parent.run_check(name, timeout_seconds=timeout)
                except ToolError as check_error:
                    checks.append({'name': name, 'passed': False, 'returncode': None,
                        'output': str(check_error), 'error': check_error.code,
                        'duration_seconds': time.monotonic() - started})
                    raise
                checked['duration_seconds'] = time.monotonic() - started
                checks.append(checked)
                if not checked['passed']:
                    raise ToolError('candidate_check_failed', f'Candidate failed configured check: {name}.')
            if deadline is not None and time.monotonic() >= deadline:
                raise ToolError('promotion_timeout', 'Verification consumed the available promotion time.')
            if any(file_hash(self.parent.policy.resolve_write(path)) != value for path, value in post_apply.items()):
                raise ToolError('check_changed_workspace', 'A configured check modified candidate source files; automatic commit is refused.')
            snapshot = self.parent.prepare_commit()
            if set(snapshot['paths']) != set(artifact['paths']):
                raise ToolError('check_changed_workspace', 'A check or external process changed additional files; automatic commit is refused.')
            committed = self.parent.create_commit(message, artifact['paths'], snapshot['fingerprint'])
        except Exception as error:
            rollback_error = None
            try:
                current_head = self.parent._run(['rev-parse', 'HEAD'])['stdout'].strip()
                current_branch = self.parent._run(['symbolic-ref', '--quiet', 'HEAD'], check=False)['stdout'].strip()
                if current_head != checkpoint['head'] or current_branch != checkpoint['branch']:
                    raise ToolError('promotion_rollback_conflict', 'Git HEAD changed during promotion; preserve files and inspect the actual commit before retrying.')
                if len(post_apply) != len(artifact['paths']):
                    raise ToolError('promotion_rollback_conflict', 'Could not capture transferred files safely; preserve them for inspection.')
                if any(file_hash(self.parent.policy.resolve_write(path)) != value for path, value in post_apply.items()):
                    raise ToolError('promotion_rollback_conflict', 'Candidate files changed after transfer; preserve them instead of overwriting concurrent changes.')
                self.parent._run(['apply', '--reverse', '--check', '--whitespace=nowarn', '-'], input_text=artifact['patch'])
                self.parent._run(['apply', '--reverse', '--whitespace=nowarn', '-'], input_text=artifact['patch'])
                record['applied'] = False
            except (ToolError, OSError) as rollback:
                rollback_error = str(rollback)
            remaining = []
            try:
                remaining = self.parent.prepare_commit()['paths']
                if rollback_error is None:
                    rollback_error = 'Candidate edits were reverted, but other workspace changes remain. Inspect configured check effects.'
            except ToolError as inspection:
                if inspection.code != 'nothing_to_commit':
                    rollback_error = rollback_error or 'Rollback state could not be verified; inspect the workspace.'
            return {'promoted': False, 'rolled_back': rollback_error is None,
                    'error': getattr(error, 'code', 'promotion_failed'),
                    'message': str(error) if isinstance(error, ToolError) else 'Local promotion failed; inspect the saved candidate.',
                    'rollback_error': rollback_error,
                    'checks': checks, 'files': remaining or (artifact['paths'] if rollback_error else []),
                    'files_changed': len(remaining or artifact['paths']) if rollback_error else 0,
                    'workspace_changed': rollback_error is not None, 'task_id': task_id}
        return {'promoted': True, 'rolled_back': False, 'workspace_changed': applied,
                'checks': checks, 'files': result['files'], 'files_changed': result['files_changed'],
                'task_id': task_id, **committed}

    def create(self, task_id):
        self.parent._require_repository()
        head = self.parent._run(['rev-parse', '--verify', 'HEAD'], check=False)
        if head['returncode']:
            raise ToolError('worker_needs_commit', 'Implementation workers need an initial Git commit. Create and commit the skeleton first, or use research mode.')
        try:
            snapshot = self.parent._commit_snapshot()
            changed = snapshot['paths']
        except ToolError as error:
            if error.code != 'nothing_to_commit':
                raise
            changed = []
        target = self.directory / task_id
        if target.exists():
            raise ToolError('worker_workspace_exists', 'This task already has a worker workspace.')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.parent._run(['worktree', 'add', '--detach', str(target), head['stdout'].strip()])
        repo = RepositoryTools(target, checks=self.parent.checks)
        repo.GIT_TIMEOUT = self.parent.GIT_TIMEOUT
        repo.CHECK_TIMEOUT = self.parent.CHECK_TIMEOUT
        # Overlay permitted dirty/staged files without changing the director's index.
        for path in changed:
            source = self.parent.policy.resolve_write(path)
            destination = repo.policy.resolve_write(path)
            if source.exists():
                file_hash(source)  # Reject links and non-files before copying.
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            elif destination.exists():
                if not destination.is_file() or destination.is_symlink():
                    raise ToolError('unsupported_worker_file', 'Cannot snapshot a non-file deletion.')
                destination.unlink()
        baseline = head['stdout'].strip()
        if changed:
            repo._run(['add', '-A', '--', *changed])
            tree = repo._run(['write-tree'])['stdout'].strip()
            # A private detached baseline: no hooks and no director index/branch mutation.
            baseline = repo._run(['-c', 'commit.gpgsign=false', 'commit-tree', tree, '-p', baseline],
                                 input_text='Orchestra worker baseline\n')['stdout'].strip()
            repo._run(['update-ref', 'HEAD', baseline])
        paths = repo._run(['ls-files', '-z'])['stdout'].split('\0')
        # Compare transfers against director bytes, which may differ from a fresh
        # checkout under Git's LF/CRLF normalization despite identical commits.
        hashes = {path: file_hash(self.parent.policy.resolve_write(path)) for path in paths
                  if path and repo.policy.allows_relative(path, write=True)}
        self.records[task_id] = {'repository': repo, 'baseline': baseline,
                                 'hashes': hashes, 'artifact': None, 'applied': False,
                                 'reviewed': False}
        return repo

    def collect(self, task_id):
        record = self.records[task_id]
        repo = record['repository']
        try:
            paths = repo.prepare_commit()['paths']
        except ToolError as error:
            if error.code != 'nothing_to_commit':
                raise
            paths = []
        # Include untracked worker files in the diff without creating user commits.
        if paths:
            repo._run(['add', '-A', '--', *paths])
            # Staging can normalize a line-ending-only status change to no diff.
            # Transfer only files that actually differ from the frozen baseline.
            staged = repo._run(['diff', '--cached', '--name-only', '--no-renames', '-z', record['baseline']])['stdout']
            paths = repo._filter_paths(staged.split('\0'))
        patch = repo._run(['diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv',
                           '--no-renames', '--no-color', record['baseline'], '--', *paths])['stdout'] if paths else ''
        expected = {path: record['hashes'].get(path) for path in paths}
        fingerprint = hashlib.sha256((record['baseline'] + '\0' + patch).encode('utf-8')).hexdigest()
        unsupported = None
        if patch:
            try:
                repo._patch_paths(patch)
            except ToolError as error:
                unsupported = str(error)
        artifact = {'patch': patch, 'paths': paths, 'expected': expected,
                    'fingerprint': fingerprint, 'unsupported': unsupported,
                    'file_patches': {path: repo._run(['diff', '--cached', '--no-ext-diff', '--no-textconv',
                        '--no-renames', '--no-color', record['baseline'], '--', path])['stdout'] for path in paths}}
        record['artifact'] = artifact
        record['reviewed'] = False
        artifacts = self.directory / 'artifacts'
        artifacts.mkdir(exist_ok=True)
        artifact_path = artifacts / f'{task_id}.json'
        artifact_path.write_text(json.dumps({'workspace': str(repo.root), 'baseline': record['baseline'],
            **artifact}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return {'workspace': str(repo.root), 'baseline': record['baseline'],
                'artifact_file': str(artifact_path),
                'changed_paths': paths, 'patch_characters': len(patch),
                'fingerprint': fingerprint, 'transfer_supported': unsupported is None,
                'transfer_limitation': unsupported, 'applied': record['applied']}

    def review(self, task_id, path=None):
        record = self.records.get(task_id)
        if not record or record['artifact'] is None:
            raise ToolError('worker_changes_unavailable', 'Wait for this implementation worker to finish before reviewing its changes.')
        artifact = record['artifact']
        patch = artifact['patch']
        if path is not None:
            safe = self.parent._relative_path(path)
            if safe not in artifact['paths']:
                raise ToolError('invalid_worker_path', 'Choose one of the returned changed_paths.')
            patch = artifact['file_patches'][safe]
        shown, truncated = self.parent._bounded(patch)
        record['reviewed'] = True
        return {'task_id': task_id, 'changed_paths': artifact['paths'], 'diff': shown,
                'truncated': truncated, 'fingerprint': artifact['fingerprint'],
                'workspace': str(record['repository'].root), 'applied': record['applied'],
                'transfer_supported': artifact['unsupported'] is None,
                'transfer_limitation': artifact['unsupported']}

    def apply(self, task_id, fingerprint):
        record = self.records.get(task_id)
        if not record or record['artifact'] is None:
            raise ToolError('worker_changes_unavailable', 'Wait for this implementation worker to finish before transferring changes.')
        artifact = record['artifact']
        if record['applied']:
            raise ToolError('worker_changes_already_applied', 'These worker changes were already applied; do not repeat the transfer.')
        if fingerprint != artifact['fingerprint']:
            raise ToolError('stale_worker_review', 'Use the fingerprint from review_task_changes.')
        if not record['reviewed']:
            raise ToolError('worker_review_required', 'Call review_task_changes before transferring this patch.')
        if artifact['unsupported']:
            raise ToolError('unsupported_worker_patch', artifact['unsupported'])
        if not artifact['patch']:
            raise ToolError('no_worker_changes', 'This worker has no file changes to transfer.')
        conflicts = [path for path, expected in artifact['expected'].items()
                     if file_hash(self.parent.policy.resolve_write(path)) != expected]
        if conflicts:
            raise ToolError('worker_changes_conflict', 'Director files changed since the worker snapshot. Inspect both versions and reconcile explicitly.', paths=conflicts)
        result = self.parent.apply_patch(artifact['patch'])
        record['applied'] = True
        return {'task_id': task_id, **result}
