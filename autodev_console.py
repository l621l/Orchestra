"""A small local status line; no model calls or intent classification."""
import json
from autodev_intent import input_command


HELP='C=continue | N: <task>=new | S=status | Q=exit | text=update current task'


def brief(app):
    if not app.state_path.is_file():
        return '[NEW] No saved task. Type a task or use N: <task>.\n'+HELP
    try:
        state=json.loads(app.state_path.read_text(encoding='utf-8'))
        if state.get('version')!=2 or state.get('workspace')!=str(app.files.root):
            raise ValueError('Invalid checkpoint')
        tasks=state['plan'];done=sum(t['status']=='done' for t in tasks)
        if state.get('mode')=='direct' or getattr(app,'direct_mode',False):
            code='DONE' if state.get('status')=='completed' else 'PAUSE'
            line = f'[{code}] Direct | Changed files: {len(state.get("modified_files", []))} | Spent: ${state.get("budget_spent", 0):.4f}/${app.options["max_task_cost_usd"]:.2f}'
            if state.get('stop_reason'):
                line += ' | Stop: ' + state['stop_reason']
            command = state.get('last_command') or {}
            if command:
                line += ' | Last command: ' + ('PASS' if command['passed'] else 'FAIL')
                if command.get('revision') != state.get('revision'):
                    line += ' (earlier revision)'
            if state.get('verified_copy'):
                line += '\nVerified copy: ' + state['verified_copy']['path']
            if state.get('checkpoint_error'):
                line += '\nCopy failed: ' + state['checkpoint_error']
            return line + '\n' + HELP
        code='DONE' if state.get('status')=='completed' else 'PLAN' if not tasks else 'PAUSE'
        progress=f'{done}/{len(tasks)}' if tasks else 'no plan yet'
        next_step='plan' if not tasks else next(('#'+str(i+1) for i,t in enumerate(tasks) if t['status']!='done'),'-')
        cap=app.options['max_task_cost_usd']
        return f'[{code}] Done: {progress} | Next: {next_step} | Spent: ${state.get("budget_spent",0):.4f}/${cap:.2f}\n'+HELP
    except (OSError,ValueError,KeyError,TypeError):
        return '[STATE_ERROR] Cannot read checkpoint. N: <task> starts new and archives it.\n'+HELP
