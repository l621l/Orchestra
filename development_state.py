"""Shared version-2 development checkpoint loading and creation."""
import json
import shutil
import uuid
from datetime import datetime, timezone

from autodev_intent import continuation_intent, input_command
from model_errors import ModelResponseError


def read_task(path, text):
    command,payload=input_command(text)
    if command in {'status','exit'}:
        raise ModelResponseError('This is a local console command, not a development request.')
    saved=None
    if path.is_file() and command not in {'new', 'debug'}:
        saved=json.loads(path.read_text(encoding='utf-8'))
    active=bool(saved and saved.get('status')!='completed')
    continuing,clarification=continuation_intent(text,active)
    return command, payload, saved, continuing, clarification


def new_task(files, path, command, payload):
    specification=payload
    if not specification:
        raise ModelResponseError('Enter the error after D: or the task after N:.')
    spec=files.policy.resolve_write('what_to_do.md')
    if command != 'debug' and spec.is_file():
        if spec.stat().st_size>100000:raise ModelResponseError('what_to_do.md exceeds 100000 bytes.')
        specification+='\nWorkspace specification (user instruction takes priority):\n'+spec.read_text(encoding='utf-8')
    if len(specification)>120000:raise ModelResponseError('Specification exceeds 120000 characters.')
    if path.is_file():
        archive=path.parent / 'autodev_history'
        archive.mkdir(exist_ok=True)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:6]
        shutil.copy2(path,archive/(path.stem+'_'+stamp+'.json'))
    return {
        'version': 2, 'task_kind': command, 'workspace': str(files.root),
        'specification': specification, 'clarifications': [], 'messages': [],
        'plan': [], 'position': 0, 'revision': 0, 'modified_files': [],
        'file_hashes': {}, 'budget_spent': 0.0, 'status': 'running',
    }
