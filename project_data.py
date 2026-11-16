"""Local Orchestra data keyed by canonical workspace path, independent of Git branches."""
import hashlib
import json
import os
from pathlib import Path

from interface_notes import atomic_text
from project_map import HEADER

JOURNAL = 'ORCHESTRA_JOURNAL.jsonl'
MAP = 'PROJECT_MAP.md'
SETTINGS = 'project-settings.json'


def project_data_path(logs, workspace):
    identity = os.path.normcase(str(Path(workspace).resolve()))
    project_id = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    return Path(logs) / 'projects' / project_id


def generated(name, content):
    if name == MAP:
        return content.startswith(HEADER)
    if name == JOURNAL:
        try:
            rows = [json.loads(line) for line in content.splitlines() if line.strip()]
            return bool(rows) and all(isinstance(row, dict) and isinstance(row.get('event'), str)
                                      and isinstance(row.get('time'), str) for row in rows)
        except ValueError:
            return False
    return False


def preserve_legacy_checkout_changes(workspace, storage, git):
    """Back up old generated edits before restoring only their tracked worktree copies."""
    restored = []
    for name in (JOURNAL, MAP):
        path = workspace / name
        if path.is_symlink() or not path.is_file():
            continue
        status = git('status', '--porcelain=v1', '--', name).stdout
        if not status.startswith(' M '):
            continue
        previous = git('show', f'HEAD:{name}', check=False)
        if previous.returncode != 0 or not generated(name, previous.stdout):
            continue
        current = path.read_text(encoding='utf-8')
        if not generated(name, current):
            continue
        storage.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(current.encode('utf-8')).hexdigest()[:16]
        atomic_text(storage / f'legacy-{digest}-{name}', current)
        if name == JOURNAL and not (storage / name).exists():
            atomic_text(storage / name, current)
        if path.read_text(encoding='utf-8') == current:
            git('restore', '--worktree', '--', name)
            restored.append(name)
    return restored
