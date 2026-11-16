"""Explicit console commands; ordinary text follows the unfinished task."""

from dataclasses import dataclass
import json
import shutil
import uuid
from datetime import datetime, timezone

from model_errors import ModelResponseError


@dataclass(frozen=True)
class TaskRequest:
    action: str
    text: str


def input_command(text):
    if isinstance(text, TaskRequest):
        return text.action, text.text
    stripped=text.strip()
    lowered=stripped.casefold()
    if lowered in {'c','с','продолжи','продолжить','continue','resume'}:return 'continue',''
    if lowered in {'s','ы','status'}:return 'status',''
    if lowered in {'q','й','exit','quit'}:return 'exit',''
    if lowered in {'n','т'}:return 'new',''
    for prefix in ('n:','т:','новая задача:','новое задание:'):
        if lowered.startswith(prefix):return 'new',stripped[len(prefix):].strip()
    return 'text',stripped


def continuation_intent(text,has_checkpoint):
    command,payload=input_command(text)
    if command in {'new', 'review'}:return False,''
    if command=='continue':return True,payload
    return bool(has_checkpoint),payload if has_checkpoint else ''


def read_task(path, text):
    command,payload=input_command(text)
    if command in {'status','exit'}:
        raise ModelResponseError('This is a local console command, not a development request.')
    saved=None
    if path.is_file() and command not in {'new', 'review'}:
        saved=json.loads(path.read_text(encoding='utf-8'))
    active=bool(saved and saved.get('status')!='completed')
    continuing,clarification=continuation_intent(text,active)
    return command, payload, saved, continuing, clarification


def new_task(files, path, command, payload):
    specification=payload
    if not specification and command != 'review':
        raise ModelResponseError('Enter the task after N:.')
    spec=files.policy.resolve_write('what_to_do.md')
    if spec.is_file():
        if spec.stat().st_size>100000:raise ModelResponseError('what_to_do.md exceeds 100000 bytes.')
        workspace_spec = spec.read_text(encoding='utf-8')
        if workspace_spec.strip():
            specification += '\nWorkspace specification (user instruction takes priority):\n' + workspace_spec
    if not specification.strip():
        raise ModelResponseError('Enter review requirements or add what_to_do.md to the workspace.')
    if len(specification)>120000:raise ModelResponseError('Specification exceeds 120000 characters.')
    if path.is_file():
        archive=path.parent / 'autodev_history'
        archive.mkdir(exist_ok=True)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:6]
        shutil.copy2(path,archive/(path.stem+'_'+stamp+'.json'))
    return {
        'version': 2, 'task_kind': command, 'workspace': str(files.root),
        'specification': specification, 'clarifications': [], 'messages': [],
        'revision': 0, 'modified_files': [],
        'file_hashes': {}, 'budget_spent': 0.0, 'status': 'running',
    }
