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
