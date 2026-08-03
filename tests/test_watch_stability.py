"""Tests for wait_until_stable - the half-uploaded-file guard in the watcher.

This replaced an unconditional `time.sleep(1)` per file, which made an archive
replay of ~9,700 files spend ~2.7 hours sleeping. The tests pin both halves of
the trade: settled files must return *fast* (the point of the change) and a file
still being written must not be declared complete (the point of the guard).
"""
import logging
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

# watch_ftproot is the only module that needs the container's environment at
# import time: watchdog, yaml, and a FileHandler on /app/logs. Stub them here
# rather than in conftest so the other test modules stay independent of it. Real
# modules win when present (container, CI with requirements installed) -- the
# stubs only cover a bare checkout, and any actual use of them raises rather
# than quietly returning a wrong value.
for _name, _attrs in (
        ('watchdog', {}),
        ('watchdog.observers', {'Observer': object}),
        ('watchdog.events', {'FileSystemEventHandler': object}),
):
    try:
        __import__(_name)
    except ModuleNotFoundError:
        _stub = types.ModuleType(_name)
        for _attr, _value in _attrs.items():
            setattr(_stub, _attr, _value)
        sys.modules[_name] = _stub

try:
    import yaml  # noqa: F401
except ModuleNotFoundError:
    def _no_yaml(*args, **kwargs):
        raise AssertionError('this test must not depend on YAML parsing')

    _yaml_stub = types.ModuleType('yaml')
    _yaml_stub.safe_load = _no_yaml
    sys.modules['yaml'] = _yaml_stub

# The module-level FileHandler('/app/logs/watcher.log') exists only in the
# container. Swap in a no-op handler for the duration of the import; the real
# handler is exercised by running the container, not by unit tests.
_real_file_handler = logging.FileHandler
logging.FileHandler = lambda *a, **k: logging.NullHandler()
try:
    from watch_ftproot import wait_until_stable
finally:
    logging.FileHandler = _real_file_handler


def _settled(tmp_path, name='20260527_a.xml', age=10.0):
    """A file whose mtime is `age` seconds in the past, as after an unzip."""
    path = tmp_path / name
    path.write_bytes(b'<xml/>')
    past = time.time() - age
    os.utime(path, (past, past))
    return path


def test_settled_file_returns_immediately(tmp_path):
    """The replay case: no sleeping at all for an already-complete file."""
    path = _settled(tmp_path)

    start = time.monotonic()
    assert wait_until_stable(path) is True
    assert time.monotonic() - start < 0.1


def test_freshly_written_file_waits_for_quiet_period(tmp_path):
    """A file touched just now is not trusted until it has been quiet."""
    path = tmp_path / 'fresh.xml'
    path.write_bytes(b'<xml/>')

    start = time.monotonic()
    assert wait_until_stable(path, quiet_seconds=0.5, timeout=5.0,
                             interval=0.05) is True
    # It had to wait out the quiet period rather than accepting immediately.
    assert time.monotonic() - start >= 0.4


def test_growing_file_is_reported_incomplete(tmp_path):
    """A file still being written must not be declared complete.

    The old check could pass a slow upload that paused ~1s between chunks; this
    writes continuously, so both old and new logic should catch it -- what is
    pinned here is that the timeout path returns False rather than True.
    """
    path = tmp_path / 'growing.xml'
    path.write_bytes(b'<')
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            with open(path, 'ab') as fh:
                fh.write(b'x')
            time.sleep(0.02)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        assert wait_until_stable(path, quiet_seconds=1.0, timeout=0.5,
                                 interval=0.05) is False
    finally:
        stop.set()
        thread.join(timeout=2)


def test_missing_file_returns_false_without_raising(tmp_path):
    assert wait_until_stable(tmp_path / 'gone.xml') is False


def test_future_mtime_falls_back_to_size_comparison(tmp_path):
    """Clock/timestamp skew must not block ingestion forever.

    An mtime in the future never satisfies the quiet-period test, so without the
    size fallback a skewed file (NFS, FTP server clock) would stall every batch
    it appears in. It should time out, see a stable size, and accept.
    """
    path = tmp_path / 'skewed.xml'
    path.write_bytes(b'<xml/>')
    future = time.time() + 3600
    os.utime(path, (future, future))

    assert wait_until_stable(path, quiet_seconds=2.0, timeout=0.3,
                             interval=0.05) is True


def test_caller_ignores_result_and_still_processes(tmp_path):
    """Documents the deliberate contract: the return value is advisory.

    _process_batch() calls this for its side effect (the wait) and processes the
    file regardless -- a truncated file fails the XML parse and stays in the
    source folder for retry, which is the desired outcome. If someone later
    makes a False return skip the file, that is a behaviour change and this test
    should be updated on purpose, not silently.
    """
    import inspect

    import watch_ftproot

    source = inspect.getsource(watch_ftproot.SDATFileHandler._process_batch)
    assert 'wait_until_stable(file_path)' in source
    assert 'if wait_until_stable' not in source
