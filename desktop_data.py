"""Per-user settings and one-time import from the repository layout."""
import json
import os
import shutil
from pathlib import Path

from interface_notes import atomic_text


class DesktopData:
    def __init__(self, source, root=None):
        self.source = Path(source)
        default = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local' / 'share')
        self.root = Path(root) if root is not None else default / 'Orchestra'
        self.logs = self.root / 'logs'
        self.config = self.root / 'config.local.json'
        self.preferences = self.root / 'desktop.json'

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(exist_ok=True)
        for name in ('settings.json', 'prompts.json', 'pricing.json'):
            target = self.root / name
            if not target.exists():
                shutil.copyfile(self.source / name, target)

    def has_legacy_data(self):
        return ((self.source / 'config.local.json').is_file()
                or (self.source / 'logs').is_dir())

    def import_legacy(self):
        for name in ('config.local.json', 'settings.json', 'prompts.json', 'pricing.json'):
            original = self.source / name
            target = self.root / name
            if original.is_file() and (not target.exists() or name != 'config.local.json'):
                atomic_text(target, original.read_text(encoding='utf-8'))
        previous = self.source / 'logs'
        if previous.is_dir():
            for original in previous.rglob('*'):
                if original.is_symlink() or not original.is_file() or original.suffix == '.tmp':
                    continue
                target = self.logs / original.relative_to(previous)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with target.open('xb') as stream:
                        stream.write(original.read_bytes())
                except FileExistsError:
                    continue

    def load_preferences(self):
        try:
            value = json.loads(self.preferences.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_preferences(self, value):
        atomic_text(self.preferences, json.dumps(value, ensure_ascii=False, indent=2))

    def history(self):
        projects = {}
        for path in sorted(self.logs.glob('*/turn_*/summary.json'), reverse=True):
            try:
                summary = json.loads(path.read_text(encoding='utf-8'))
                workspace = summary['workspace']
                if not isinstance(workspace, str) or not Path(workspace).is_absolute():
                    continue
            except (OSError, ValueError, TypeError, KeyError):
                continue
            projects.setdefault(workspace, []).append((path.parent, summary))
        return projects
