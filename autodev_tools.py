"""Small workflow tools and bounded execution of project runtimes."""
import os
import shutil
import sys
from tools import definition
from tool_errors import ToolError
from autodev_evidence import tracked_python, referenced_project_files


TASK_SCHEMA = {'type':'object','additionalProperties':False,
    'properties':{'id':{'type':'string','minLength':1,'maxLength':32},
                  'goal':{'type':'string','minLength':1,'maxLength':400},
                  'acceptance':{'type':'array','minItems':1,'maxItems':4,'items':{'type':'string','minLength':1,'maxLength':160}},
                  'files':{'type':'array','maxItems':10,'items':{'type':'string','minLength':1,'maxLength':180}},
                  'depends_on':{'type':'array','maxItems':24,'items':{'type':'string','minLength':1,'maxLength':32}}},
    'required':['id','goal','acceptance','files','depends_on']}

WORKFLOW_TOOLS = [
    definition('remember_work','Save diagnosis and next action for current task; does not count as implementation progress.',
        {'diagnosis':{'type':'string','maxLength':600},
         'next_action':{'type':'string','minLength':1,'maxLength':400}},['diagnosis','next_action']),
    definition('set_plan','Save small ordered tasks covering the full specification. First: runnable path.',
        {'tasks':{'type':'array','minItems':1,'maxItems':24,'items':TASK_SCHEMA}},['tasks']),
    definition('read_spec','Read full saved specification in pages.',
        {'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':8000}},[]),
    definition('run_command','Run project command without shell; bounded time/output. Use {python} for Python.',
        {'argv':{'type':'array','minItems':1,'maxItems':64,'items':{'type':'string','minLength':1,'maxLength':8000}},
         'purpose':{'type':'string','enum':['inspect','build','verify'],'description':'Default inspect; verify must execute project code.'},
         'criteria':{'type':'array','maxItems':4,'items':{'type':'integer','minimum':1,'maximum':4},'description':'1-based current acceptance criteria checked.'},
         'timeout_seconds':{'type':'integer','minimum':1,'maximum':30}},['argv']),
    definition('finish_task','Complete current task only after a successful relevant command after its latest edit.',
        {'id':{'type':'string','minLength':1,'maxLength':32},'summary':{'type':'string','minLength':1,'maxLength':800}},['id','summary']),
]


def validate_plan(tasks, policy):
    seen = set()
    for task in tasks:
        if task['id'] in seen or any(key not in seen for key in task['depends_on']):
            raise ToolError('invalid_plan','IDs must be unique; dependencies must precede each task.')
        if not task['goal'].strip() or any(not s.strip() for s in task['acceptance']):
            raise ToolError('invalid_plan','Goals and acceptance must not be blank.')
        for path in task['files']:
            policy.resolve_write(path)
        seen.add(task['id'])
    return tasks


def run_command(repository, argv, timeout_seconds=15, purpose='inspect', criteria=None):
    executable = argv[0]
    name = os.path.basename(executable).lower()
    if executable in {'{python}','python','python3','py',sys.executable}:
        command = [sys.executable,*argv[1:]]
    elif name in {'node','node.exe','npm','npm.cmd','dotnet','dotnet.exe','go','go.exe','cargo','cargo.exe','java','java.exe'} and executable == name:
        resolved = shutil.which(executable)
        if not resolved:
            raise ToolError('missing_runtime','Runtime is not installed: '+name)
        command = [resolved,*argv[1:]]
    else:
        raise ToolError('unsupported_command','Use python/node/npm/dotnet/go/cargo/java; shell executables and Git are disabled.')
    if any('\x00' in item for item in command):
        raise ToolError('invalid_command','Command arguments must not contain NUL.')
    # Reuse bounded output capture, timeout and process-tree cleanup already used by checks.
    key = '__autodev_runtime__'
    previous = repository.checks.get(key)
    previous_timeout = repository.CHECK_TIMEOUT
    repository.CHECK_TIMEOUT = timeout_seconds
    def invoke(actual):
        repository.checks[key]=actual
        return repository.run_check(key)
    try:
        if purpose in {'verify','build'} and command[0]==sys.executable:
            result=tracked_python(repository,command,invoke)
        else:
            result=invoke(command)
            result['executed_project_files']=referenced_project_files(repository,argv) if purpose=='verify' else []
            result['compiled_project_files']=referenced_project_files(repository,argv) if purpose=='build' else []
        result['argv'] = argv
        result['purpose']=purpose
        result['criteria']=criteria or []
        result['qualifies_as_verification']=bool(result['passed'] and purpose in {'build','verify'} and
            (result['executed_project_files'] if purpose=='verify' else result['compiled_project_files']))
        return result
    finally:
        repository.CHECK_TIMEOUT = previous_timeout
        if previous is None:
            repository.checks.pop(key,None)
        else:
            repository.checks[key] = previous
