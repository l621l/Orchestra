"""A small local status line with no model calls."""
from autodev_intent import input_command
from workspace_status import workspace_status


HELP = 'C=continue | N: <task>=new | D: <error>=debug | S=status | Q=exit | text=update current task'


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
        mode = 'Debug' if state['task_kind'] == 'debug' else 'Direct'
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
    count = state['total_tasks']
    progress = f'{state["completed_tasks"]}/{count}' if count else 'no plan yet'
    next_task = state['next_task']
    next_step = 'plan' if not count else f'#{next_task}' if next_task else '-'
    return f'[{code}] Done: {progress} | Next: {next_step} | Spent: {spent}\n' + HELP
