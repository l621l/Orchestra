"""Explicit, finite routes composed from independently runnable development blocks."""
from dataclasses import dataclass
from typing import Protocol
import time

from budget_git import requested_publication
from completion_review import CompletionReviewBlock
from orchestra import parse_task_report
from task_state import input_command


@dataclass
class RouteContext:
    engine: object
    usage: dict
    trace: object
    task_id: str
    text: str


class DevelopmentBlock(Protocol):
    name: str

    async def run(self, context: RouteContext, previous: dict | None) -> dict:
        """Return the standard task result; partial results stop the route."""
        ...


class ImplementationBlock:
    name = 'implement'

    async def run(self, context, previous):
        engine = context.engine
        return await engine.run('worker', engine._state['messages'],
            {'calls': engine.settings['execution']['max_api_calls_per_turn']},
            context.usage, context.trace, context.task_id,
            max_calls=engine.settings['execution']['max_steps_per_agent'],
            publish_on_cost_stop=requested_publication(input_command(context.text)[1]))


BLOCKS = {'implement': ImplementationBlock, 'review': CompletionReviewBlock}


def validate_route(route, *, standalone_review=False):
    first_block = 'review' if standalone_review else 'implement'
    if (not isinstance(route, list) or not route or route[0] != first_block
            or (standalone_review and route != ['review'])
            or any(not isinstance(name, str) or name not in BLOCKS for name in route)
            or len(set(route)) != len(route)):
        raise ValueError(f'Route must start with {first_block} and contain known blocks without repeats')
    return route


async def run_route(route, context):
    engine = context.engine
    validate_route(route, standalone_review=engine._state.get('task_kind') == 'review')
    steps = []
    engine._state['route'] = {'blocks': list(route), 'steps': steps, 'status': 'running'}
    result = None
    try:
        for name in route:
            step = {'block': name, 'status': 'running'}
            steps.append(step)
            engine.save_state()
            context.trace.event('route_block_started', task_id=context.task_id, block=name)
            started = time.monotonic()
            metrics = {'edits': 0, 'files': set(), 'lines_added': 0, 'lines_removed': 0}
            engine._stage_edits = metrics
            status = 'interrupted'
            try:
                result = await BLOCKS[name]().run(context, result)
                status = result['status']
            finally:
                engine._stage_edits = None
                entry = context.usage.get('reviewer' if name == 'review' else 'writer')
                if entry is not None:
                    entry.update(metrics, files=sorted(metrics['files']), stage=name,
                                 elapsed_seconds=time.monotonic() - started, stage_status=status)
            report = parse_task_report(result['answer'])
            complete = (result['status'] == 'completed' and isinstance(report, dict)
                        and report.get('done') is True and report.get('state') == 'complete')
            step['status'] = 'completed' if complete else 'partial'
            context.trace.event('route_block_finished', task_id=context.task_id, **step)
            if not complete:
                result['status'] = 'partial'
                break
        engine._state['route']['status'] = result['status']
        result['route'] = {'blocks': list(route), 'steps': [dict(step) for step in steps]}
        return result
    finally:
        if steps and steps[-1]['status'] == 'running':
            steps[-1]['status'] = 'interrupted'
            engine._state['route']['status'] = 'interrupted'
        engine.save_state()
