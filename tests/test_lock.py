"""Test the output-directory lock of draw_figure.py on this platform.

A second process is refused at once while the first holds the lock, and the lock can be taken again after the first
releases it normally and after it is killed.

Usage: python tests/test_lock.py
"""
import multiprocessing
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'reaction-draw' / 'scripts'))
from draw_figure import _lock  # noqa: E402


def hold(path, ready, release):
    handle = _lock(path)
    ready.set()
    if not release.wait(30):
        raise RuntimeError('holder timed out')
    handle.close()


def take(path, wait):
    """Take and release the lock, retrying for up to `wait` seconds (the OS may release a killed holder's lock late)."""
    deadline = time.monotonic() + wait
    while True:
        try:
            _lock(path).close()
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def main():
    ctx = multiprocessing.get_context('spawn')
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / '.draw.lock'
        for killed in (False, True):
            ready, release = ctx.Event(), ctx.Event()
            holder = ctx.Process(target=hold, args=(path, ready, release))
            holder.start()
            try:
                assert ready.wait(30), 'the holder did not take the lock'
                started = time.monotonic()
                try:
                    _lock(path).close()
                    raise AssertionError('a second process took a held lock')
                except BlockingIOError:
                    pass
                assert time.monotonic() - started < 0.5, 'a held lock was not refused at once'
                if killed:
                    holder.kill()
                else:
                    release.set()
                holder.join(30)
                assert (holder.exitcode != 0) if killed else (holder.exitcode == 0), 'holder exit code %s' % holder.exitcode
                take(path, 5 if killed else 0)
            finally:
                if holder.is_alive():
                    holder.kill()
                holder.join(30)
        assert path.is_file() and path.stat().st_size == 0, 'the lock file was written'
    print('lock test passed: refused while held; free after release and after kill')


if __name__ == '__main__':
    main()
