"""Complete an explicitly requested, prepared Git publication on a cost stop."""
import asyncio
import re

from orchestra_core.models.model_errors import error_description
from orchestra_core.workspace.tool_errors import ToolError


def requested_publication(text):
    # Accept an affirmative command in the user's text, excluding quoted examples,
    # questions, and negated or conditional publication instructions.
    commands = [part.strip().casefold().rstrip('.!') for part in re.split(r'[.!\n;]+', text)]
    return '?' not in text and any(re.fullmatch(
        r'(?:закоммить(?:те)?(?: (?:изменения|правки))? и (?:запушь(?:те)?|отправь(?:те) на github)|'
        r'(?:git )?commit(?: (?:the )?changes)? and (?:git )?push)'
        r'(?: (?:после проверки|after verification|текущую ветку|the current branch))?', command,
    ) for command in commands)


class PreparedPublication:
    def __init__(self, repository, trace, task_id):
        self.repository = repository
        self.trace = trace
        self.task_id = task_id
        self.plan = None
        self.verified_fingerprint = None
        try:
            self.initial_paths = set(repository.prepare_commit()['paths'])
        except ToolError as error:
            self.initial_paths = set() if error.code == 'nothing_to_commit' else None
        except OSError:
            self.initial_paths = None

    def checked(self, verified):
        self.verified_fingerprint = None
        if verified and self.initial_paths is not None:
            try:
                self.verified_fingerprint = self.repository.prepare_commit()['fingerprint']
            except (ToolError, OSError):
                pass

    def prepare(self, args, snapshot, state, verified):
        self.plan = None
        paths = args.get('paths')
        message = args.get('message')
        if (self.initial_paths is None or not paths or not message or not verified
                or state.edit_errors or snapshot['staged_changes_present']
                or snapshot['fingerprint'] != self.verified_fingerprint):
            return
        selected = set(paths)
        if (selected & self.initial_paths or not selected <= state.changed
                or not selected <= set(snapshot['paths'])):
            return
        self.plan = (state.revision, paths, message, snapshot['fingerprint'])

    @staticmethod
    async def run_git(action, *args, **kwargs):
        operation = asyncio.create_task(asyncio.to_thread(action, *args, **kwargs))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            await operation
            raise

    async def finish(self, state, verified):
        if (not self.plan or not verified or state.edit_errors or self.plan[0] != state.revision
                or self.plan[3] != self.verified_fingerprint):
            return ''
        _, paths, message, fingerprint = self.plan
        try:
            self.trace.progress('tool', name='create_commit')
            commit = await self.run_git(self.repository.create_commit, message, paths, fingerprint)
        except (ToolError, OSError) as error:
            detail = error_description(error)
            self.trace.event('budget_git_commit_failed', task_id=self.task_id, reason=detail)
            return ' The prepared commit was not created. ' + detail
        commit_id = commit.get('commit')
        self.trace.event('budget_git_commit', task_id=self.task_id, commit=commit_id, paths=commit['paths'])
        if not commit_id:
            return ' Commit created locally. Git did not confirm the ID for push.'
        try:
            self.trace.progress('tool', name='git_push')
            published = await self.run_git(self.repository.git_push, expected_head=commit_id)
        except (ToolError, OSError) as error:
            detail = error_description(error)
            self.trace.event('budget_git_push_failed', task_id=self.task_id, commit=commit_id, reason=detail)
            return f' Commit {commit_id[:8]} saved locally. Push failed. ' + detail
        self.trace.event('budget_git_push', task_id=self.task_id, commit=commit_id, **published)
        return f" Commit {commit_id[:8]} pushed to {published['remote']}/{published['branch']}."
