"""File-based mutex so only one process/thread talks to Word COM at a time."""
import os
import tempfile
import time
from contextlib import contextmanager

LOCK_PATH = os.path.join(tempfile.gettempdir(), "c2c_word_com.lock")


@contextmanager
def word_lock(timeout: int = 600, stale_after: int = 300, poll: float = 1.0):
    """Acquire an exclusive lock file. A lock older than `stale_after` seconds is treated
    as abandoned (crashed process) and removed."""
    start = time.time()
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK_PATH) > stale_after:
                    os.remove(LOCK_PATH)
                    continue
            except OSError:
                pass
            if time.time() - start > timeout:
                raise TimeoutError("Could not acquire Word COM lock")
            time.sleep(poll)
    try:
        yield
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
