"""Application entry point for executing Orchestra turns without a terminal."""
import asyncio
from pathlib import Path

from task_state import TaskRequest, input_command
from direct_dev import DirectDevelopment
from model_errors import error_description
from orchestra_config import load_configuration
from project_data import SETTINGS, project_data_path
from project_usage import record_usage
from turn_trace import TurnTrace
from workspace_lease import WorkspaceBusy, workspace_lease
from workspace_status import workspace_status


class OrchestraApp:
    """One running turn per instance, with a shared workspace and checkpoint."""

    def __init__(self, engine):
        self.engine = engine
        self.running = False

    @classmethod
    def from_directory(cls, directory, workspace, *, dashboard=None, logs_path=None):
        directory = Path(directory)
        logs = directory / 'logs' if logs_path is None else logs_path
        config, settings, prompts, pricing = load_configuration(
            directory, project_data_path(logs, workspace) / SETTINGS)
        engine = DirectDevelopment(config, workspace, settings, prompts, pricing,
                                   dashboard=dashboard,
                                   logs_path=logs)
        return cls(engine)

    def status(self):
        running = self.running
        if not running:
            try:
                with workspace_lease(self.engine.files.root):
                    state = workspace_status(self.engine, interrupted=True)
            except WorkspaceBusy:
                running = True
        if running:
            state = workspace_status(self.engine)
        return {**state, 'running': running}

    async def new_task(self, text, *, on_progress=None):
        return await self._submit(TaskRequest('new', text.strip()), on_progress=on_progress)

    async def review_task(self, text='', *, on_progress=None):
        return await self._submit(TaskRequest('review', text.strip()), on_progress=on_progress)

    async def continue_task(self, clarification='', *, on_progress=None):
        return await self._submit(TaskRequest('continue', clarification.strip()), on_progress=on_progress)


    async def run(self, text, *, on_event=None):
        """Run a task and return its result, usage and trace path.

        The optional synchronous callback receives copies of the trace events.
        Cancelling the awaiting task closes the trace and propagates cancellation.
        """
        return await self._submit(text, on_event=on_event)

    async def _submit(self, text, *, on_event=None, on_progress=None):
        action, payload = input_command(text)
        if action in {'status', 'exit'} or (action not in {'continue', 'review'} and not payload):
            raise ValueError('Enter a development request.')
        if self.running:
            raise RuntimeError('This workspace session already has a running turn.')
        self.running = True
        try:
            with workspace_lease(self.engine.files.root):
                return await self._run_turn(text, payload or action, on_event, on_progress)
        finally:
            self.running = False

    async def _run_turn(self, text, description, on_event, on_progress):
        engine = self.engine
        dashboard = engine.dashboard
        usage = {}
        trace = TurnTrace(engine.logs_path, description, engine.files.root,
                          on_event=on_event, on_progress=on_progress)
        root_task = trace.start_task('worker', description)
        status = 'failed'
        error_type = None
        usage_error = None
        try:
            trace.progress('started', topic=trace.topic)
            dashboard.start(trace.turn_id, trace.topic, output_limit=engine.total_output_limit)
            result = await engine.run_user_task(text, usage, trace, root_task)
            trace.event('workspace_result', modified_files=result['modified_files'],
                        verification=result['verification'], chain_state=result.get('chain_state'),
                        blocker=result.get('blocker'), iterations=result.get('iterations'))
            trace.save_final_answer(result['answer'])
            trace.finish_task(root_task, result['status'])
            status = result['status']
            if status == 'partial':
                dashboard.finish('worker', 'partial', 'Work incomplete; see report')
            else:
                dashboard.finish('worker', 'done', 'Task completed')
        except (asyncio.CancelledError, KeyboardInterrupt) as error:
            status = 'cancelled'
            error_type = type(error).__name__
            trace.finish_task(root_task, status, error_type)
            dashboard.finish('worker', 'failed', status)
            if isinstance(error, asyncio.CancelledError):
                raise
            result = {'status': status}
        except Exception as error:
            status = 'failed'
            error_type = type(error).__name__
            description = error_description(error)
            trace.error_message = description
            trace.event('turn_error', error_type=error_type, message=description)
            trace.finish_task(root_task, status, error_type)
            dashboard.finish('worker', 'failed', error_type)
            result = {'status': status, 'error_type': error_type, 'error': description}
        finally:
            try:
                dashboard.stop()
            finally:
                costs = [engine.estimated_cost(entry) for entry in usage.values()]
                trace.cost = {
                    'estimated_usd': sum(cost for cost in costs if cost is not None),
                    'complete': all(cost is not None and entry.get('reported_calls', 0) == entry['api_calls']
                                    for cost, entry in zip(costs, usage.values())),
                }
                if usage:
                    try:
                        record_usage(engine.project_data, list(usage.values()), costs,
                                     legacy_workspace=engine.files.root, task=description,
                                     run_id=trace.turn_id, status=status)
                        usage_error = None
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        usage_error = error
                if usage_error is not None:
                    message = error_description(usage_error)
                    trace.event('project_usage_error', message=message)
                    trace.progress('project_usage_error', message=message)
                trace.finish(status, error_type)
                trace.progress('stopped', status=status, reason=result.get('stop_reason', error_type)
                               if status not in {'cancelled', 'failed'} else error_type,
                               cost=trace.cost)
        return {**result, 'turn_id': trace.turn_id, 'log_directory': str(trace.directory),
                'usage': usage}
