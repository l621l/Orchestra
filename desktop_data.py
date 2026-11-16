"""Per-user settings and one-time import from the repository layout."""
import json
import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile

from desktop_options import DEFAULT_CONFIG
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

    def initialize_connection(self):
        try:
            with self.config.open('x', encoding='utf-8') as stream:
                json.dump(DEFAULT_CONFIG, stream, ensure_ascii=False, indent=2)
        except FileExistsError:
            pass

    def import_legacy(self):
        for name in ('config.local.json', 'settings.json', 'prompts.json', 'pricing.json'):
            original = self.source / name
            target = self.root / name
            if original.is_file() and (
                not target.exists() or name != 'config.local.json' or self.has_default_connection()
            ):
                atomic_text(target, original.read_text(encoding='utf-8'))
        previous = self.source / 'logs'
        if previous.is_dir() and not self.logs.resolve().is_relative_to(previous.resolve()):
            self.import_logs(previous)

    def import_logs(self, previous):
        pending = [(previous, self.logs)]
        while pending:
            folder, destination = pending.pop()
            try:
                existing = set(os.listdir(destination))
                ready = True
            except FileNotFoundError:
                existing = set()
                ready = False
            with os.scandir(folder) as entries:
                for entry in entries:
                    name = entry.name
                    # Older versions stored project snapshots alongside the logs.
                    if folder == previous and (name == 'verified_runs' or name.startswith('before-')):
                        continue
                    if name.endswith('.tmp'):
                        continue
                    target = destination / name
                    if entry.is_dir(follow_symlinks=False):
                        if not getattr(entry, 'is_junction', lambda: False)():
                            pending.append((Path(entry.path), target))
                    elif name not in existing and entry.is_file(follow_symlinks=False):
                        if not ready:
                            destination.mkdir(parents=True, exist_ok=True)
                            ready = True
                        self.copy_log(entry.path, target)

    @staticmethod
    def copy_log(original, target):
        temporary = None
        try:
            with open(original, 'rb') as source, NamedTemporaryFile(
                dir=target.parent, suffix='.tmp', delete=False
            ) as stream:
                temporary = Path(stream.name)
                shutil.copyfileobj(source, stream)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def has_default_connection(self):
        try:
            return json.loads(self.config.read_text(encoding='utf-8')) == DEFAULT_CONFIG
        except (OSError, ValueError):
            return False

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
