"""Cross-process workspace lease released by the OS on exit."""
import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class WorkspaceBusy(RuntimeError):
    pass


@contextmanager
def workspace_lease(workspace):
    key = hashlib.sha256(os.path.normcase(str(workspace.resolve())).encode()).hexdigest()
    folder = Path(tempfile.gettempdir()) / 'orchestra-locks'
    folder.mkdir(exist_ok=True)
    with (folder / key).open('a+b') as handle:
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise WorkspaceBusy('This workspace already has a running task.') from error
        yield
