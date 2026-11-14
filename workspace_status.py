"""Read-only view of the saved development task for application frontends."""
import json


def workspace_status(engine, *, interrupted=False):
    if not engine.state_path.is_file():
        return {'status': 'new'}
    try:
        state = json.loads(engine.state_path.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('version') != 2
                or state.get('workspace') != str(engine.files.root)):
            raise ValueError('Invalid checkpoint')
        tasks = state['plan']
        done = sum(task['status'] == 'done' for task in tasks)
        next_task = next((index for index, task in enumerate(tasks, 1)
                          if task['status'] != 'done'), None)
        direct = state.get('mode') == 'direct' or getattr(engine, 'direct_mode', False)
        status = ('done' if state.get('status') == 'completed'
                  else 'plan' if not direct and not tasks else 'pause')
        command = state.get('last_command')
        return {
            'status': status,
            'mode': 'direct' if direct else 'planned',
            'task_kind': state.get('task_kind'),
            'completed_tasks': done,
            'total_tasks': len(tasks),
            'next_task': next_task,
            'modified_file_count': len(state.get('modified_files', [])),
            'budget_spent': state.get('budget_spent', 0),
            'budget_limit': engine.options['max_task_cost_usd'],
            'stop_reason': ('interrupted' if interrupted and state.get('status') == 'running'
                            else state.get('stop_reason')),
            'last_command': {'passed': command['passed'],
                             'stale': command.get('revision') != state.get('revision')}
                            if direct and command else None,
            'verified_copy': state.get('verified_copy'),
            'checkpoint_error': state.get('checkpoint_error'),
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'status': 'state_error'}
