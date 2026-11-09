"""Keep a local copy of the last version that ran successfully."""
import os
from pathlib import Path
import shutil
import tempfile
import uuid


def save_verified_copy(policy, logs_path):
    root = policy.root
    destination = logs_path / 'verified_runs'
    destination.mkdir(parents=True, exist_ok=True)
    size = count = 0
    with tempfile.TemporaryDirectory(dir=destination, prefix='.copy-') as temporary:
        staged = Path(temporary) / 'project'
        staged.mkdir()
        for directory, dirs, files in os.walk(root):
            folder = Path(directory)
            relative = folder.relative_to(root)
            dirs[:] = [name for name in dirs
                       if policy.parts_allowed((relative / name).parts)
                       and not (folder / name).is_symlink()
                       and not (folder / name).is_junction()
                       and not destination.resolve().is_relative_to((folder / name).resolve())]
            for name in files:
                source = folder / name
                path = relative / name
                if not policy.parts_allowed(path.parts) or source.is_symlink() or not source.is_file():
                    continue
                count += 1
                size += source.stat().st_size
                if count > 2000 or size > 100 * 1024 * 1024:
                    raise OSError('Verified copy exceeds 2000 files or 100 MiB.')
                target = staged / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        published = destination / uuid.uuid4().hex
        staged.rename(published)
        return published
