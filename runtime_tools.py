"""Evidence that a verification command actually reached project code."""
import json
import os
from pathlib import Path
import sys
import tempfile


WRAPPER = r'''
import sys, os, json, runpy
root = os.path.normcase(os.path.abspath(ROOT)) + os.sep
executed, compiled = set(), set()
def local(filename):
    if not isinstance(filename, str) or filename.startswith('<'):
        return None
    absolute = os.path.normcase(os.path.abspath(filename))
    if not absolute.startswith(root):
        return None
    relative = os.path.relpath(absolute, root)
    if any(p in {'.venv','venv','node_modules','__pycache__'} for p in relative.split(os.sep)):
        return None
    return relative.replace(os.sep, '/')
def profile(frame, event, arg):
    if event == 'call':
        path = local(frame.f_code.co_filename)
        if path: executed.add(path)
def audit(event, args):
    if event == 'compile' and len(args) > 1:
        path = local(args[1])
        if path: compiled.add(path)
args = sys.argv[1:]
while args and args[0] in {'-B','-u','-O','-OO'}:
    args.pop(0)
sys.setprofile(profile)
sys.addaudithook(audit)
try:
    if args and args[0] == '-c':
        code = args[1]; sys.argv = ['-c'] + args[2:]
        exec(compile(code, '<string>', 'exec'), {'__name__':'__main__'})
    elif args and args[0] == '-m':
        module = args[1]; sys.argv = [module] + args[2:]
        runpy.run_module(module, run_name='__main__', alter_sys=True)
    elif args and not args[0].startswith('-'):
        script = args[0]; sys.argv = args
        sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
        runpy.run_path(script, run_name='__main__')
    else:
        raise SystemExit('Use a Python script, -m module or -c code.')
finally:
    sys.setprofile(None)
    with open(REPORT, 'w', encoding='utf-8') as stream:
        json.dump({'executed':sorted(executed),'compiled':sorted(compiled)}, stream)
'''


def tracked_python(repository,argv,invoke):
    with tempfile.TemporaryDirectory(prefix='orchestra-evidence-') as folder:
        report=Path(folder)/'files.json'
        wrapper=WRAPPER.replace('ROOT',repr(str(repository.root))).replace('REPORT',repr(str(report)))
        result=invoke([sys.executable,'-B','-c',wrapper,*argv[1:]])
        try:
            observed=json.loads(report.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            observed={}
        result['executed_project_files']=observed.get('executed',[])
        result['compiled_project_files']=observed.get('compiled',[])
        return result


def referenced_project_files(repository,argv):
    files=[]
    for arg in argv[1:]:
        if arg.startswith('-'):continue
        try:
            path=repository.policy.resolve_read(arg)
            if path.is_file():files.append(path.relative_to(repository.root).as_posix())
        except (ValueError,OSError):
            continue
    # npm scripts execute the workspace package; package inspection alone is not verification.
    if os.path.basename(argv[0]).lower() in {'npm','npm.cmd'} and len(argv)>2 and argv[1]=='run':
        manifest=repository.root/'package.json'
        if manifest.is_file():
            try:
                if argv[2] in json.loads(manifest.read_text(encoding='utf-8')).get('scripts',{}):files.append('package.json')
            except (OSError,ValueError):pass
    return sorted(set(files))


"""Small workflow tools and bounded execution of project runtimes."""
import os
import shutil
import sys
from tools import definition
from tool_errors import ToolError


WORKFLOW_TOOLS = [
    definition('run_command','Run project command without shell; bounded time/output. Use {python} for Python.',
        {'argv':{'type':'array','minItems':1,'maxItems':64,'items':{'type':'string','minLength':1,'maxLength':8000}},
         'purpose':{'type':'string','enum':['inspect','build','verify'],'description':'Default inspect; verify must execute project code.'},
         'criteria':{'type':'array','maxItems':4,'items':{'type':'integer','minimum':1,'maximum':4},'description':'1-based current acceptance criteria checked.'},
         'timeout_seconds':{'type':'integer','minimum':1,'maximum':30}},['argv']),
]


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
