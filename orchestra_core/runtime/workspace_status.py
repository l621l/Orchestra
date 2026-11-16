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
        status = 'done' if state.get('status') == 'completed' else 'pause'
        command = state.get('last_command')
        return {
            'status': status,
            'mode': 'direct',
            'task_kind': state.get('task_kind'),
            'modified_file_count': len(state.get('modified_files', [])),
            'budget_spent': state.get('budget_spent', 0),
            'budget_limit': engine.options['max_task_cost_usd'],
            'stop_reason': ('interrupted' if interrupted and state.get('status') == 'running'
                            else state.get('stop_reason')),
            'last_command': {'passed': command['passed'],
                             'stale': command.get('revision') != state.get('revision')}
                            if command else None,
            'verified_copy': state.get('verified_copy'),
            'checkpoint_error': state.get('checkpoint_error'),
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'status': 'state_error'}


HELP = 'C=continue | N: <task>=new | S=status | Q=exit | text=update current task'


def brief(app):
    return format_status(workspace_status(app))


def format_status(state):
    code = state['status'].upper()
    if code == 'NEW':
        return '[NEW] No saved task. Type a task or use N: <task>.\n' + HELP
    if code == 'STATE_ERROR':
        return '[STATE_ERROR] Cannot read checkpoint. N: <task> starts new and archives it.\n' + HELP
    spent = f'${state["budget_spent"]:.4f}/${state["budget_limit"]:.2f}'
    if state['mode'] == 'direct':
        mode = 'Direct'
        line = f'[{code}] {mode} | Changed files: {state["modified_file_count"]} | Spent: {spent}'
        if state['stop_reason']:
            line += ' | Stop: ' + state['stop_reason']
        command = state['last_command']
        if command:
            line += ' | Last command: ' + ('PASS' if command['passed'] else 'FAIL')
            if command['stale']:
                line += ' (earlier revision)'
        if state['verified_copy']:
            line += '\nVerified copy: ' + state['verified_copy']['path']
        if state['checkpoint_error']:
            line += '\nCopy failed: ' + state['checkpoint_error']
        return line + '\n' + HELP
