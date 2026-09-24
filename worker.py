"""Bounded controller watchdog, not a hard filesystem/process sandbox.

Workspace/TMPDIR limits are sampled: writers can overshoot between scans, escape
process groups, or write elsewhere unless deployment supplies OS isolation/quotas.
No workspace is deleted, and controller output is never persisted or disclosed.
"""
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

MAX_ARTIFACT = 32768
MAX_OUTPUT = 65536
MAX_WORKSPACE_FILES = 4096
MAX_WORKSPACE_BYTES = 64 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_directory(path):
    """Walk from / with dirfds so no ancestor symlink can be followed."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('unsafe path')
    fd = os.open('/', _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def bounded_read(path, limit=MAX_ARTIFACT):
    """Read only a single-linked regular UTF-8 file via no-follow descriptors."""
    parent = fd = None
    try:
        path = Path(path)
        if limit < 0 or '..' in path.parts:
            return None
        parent = _open_directory(path.parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                     os.O_CLOEXEC, dir_fd=parent)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            return None
        data = bytearray()
        while len(data) <= limit:
            block = os.read(fd, min(8192, limit + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        after = os.fstat(fd)
        if (len(data) > limit or after.st_nlink != 1 or after.st_size != len(data) or
                (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            return None
        return data.decode('utf-8')
    except (OSError, ValueError, UnicodeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)


def _within_budget(roots, max_files, max_bytes, deadline, cancelled):
    """Bound traversal entries, depth, time, and descriptors; never follow links."""
    count = size = 0
    scan_deadline = min(deadline, time.monotonic() + 0.1)

    def walk(fd, depth):
        nonlocal count, size
        if depth > 32:
            return False
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > max_files or cancelled.is_set() or time.monotonic() >= scan_deadline:
                    return False
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    size += info.st_size
                    if size > max_bytes or info.st_nlink != 1:
                        return False
                elif stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
                    try:
                        if not walk(child, depth + 1):
                            return False
                    finally:
                        os.close(child)
                else:
                    return False
        return True

    try:
        for root in roots:
            fd = _open_directory(root)
            try:
                if not walk(fd, 0):
                    return False
            finally:
                os.close(fd)
        return True
    except (OSError, ValueError):
        return False


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _json_result(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('invalid constant')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)


class SubprocessRunner:
    def __init__(self, config, *, controller_path=None, executable=None,
                 max_output=MAX_OUTPUT, max_workspace_files=MAX_WORKSPACE_FILES,
                 max_workspace_bytes=MAX_WORKSPACE_BYTES, term_grace=0.2):
        # Overrides are dependency-injection seams for synthetic subprocess tests.
        # Production uses the installed doer_loop.cli module and this interpreter.
        self.config = config
        self.controller_path = Path(controller_path).absolute() if controller_path else None
        self.executable = executable or sys.executable
        self.max_output = max_output
        self.max_workspace_files = max_workspace_files
        self.max_workspace_bytes = max_workspace_bytes
        self.term_grace = max(0.01, min(term_grace, 2.0))
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._pgid = None

    def cancel(self):
        """Latch shutdown and immediately TERM an active process group.

        The active watchdog escalates to KILL within term_grace; this runner is
        intentionally not reusable after shutdown cancellation.
        """
        with self._lock:
            self._cancelled.set()
            if self._pgid is not None:
                _signal_group(self._pgid, signal.SIGTERM)

    def _env(self, temporary):
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin',
               'HOME': str(self.config.hermes_home),
               'HERMES_HOME': str(self.config.hermes_home), 'TMPDIR': str(temporary),
               'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
               'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
               'OPENBLAS_NUM_THREADS': '1', 'TORCH_NUM_THREADS': '1',
               'DOER_TORCH_THREADS': '1', 'TOKENIZERS_PARALLELISM': 'false'}
        model_dir = os.environ.get('DOER_LAYA_MODEL_DIR')
        if model_dir:
            env.update(DOER_LAYA_MODEL_DIR=model_dir, HF_HUB_OFFLINE='1',
                       TRANSFORMERS_OFFLINE='1')
        return env

    def __call__(self, job_id, body, workspace):
        if not self._run_lock.acquire(blocking=False):
            return 'failed', None
        try:
            return self._run(body, Path(workspace).absolute())
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return 'failed', None
        finally:
            self._run_lock.release()

    def _run(self, body, workspace):
        if self._cancelled.is_set():
            return 'cancelled', None
        deadline = time.monotonic() + float(self.config.timeout)
        relative = body['verify_file']
        if (not isinstance(relative, str) or not relative or relative.startswith('/') or
                any(p in ('', '.', '..') for p in relative.split('/')) or
                '\\' in relative or '\x00' in relative):
            return 'failed', None
        scratch = Path.home() / '.hermes' / '.scratch'
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Keep diagnostics/work files, never remove user workspace contents.
        temporary = Path(tempfile.mkdtemp(prefix='doer-worker-', dir=scratch))
        roots = (workspace, temporary)
        if not _within_budget(roots, self.max_workspace_files, self.max_workspace_bytes,
                              deadline, self._cancelled):
            return 'failed', None
        c = self.config
        controller = [str(self.controller_path)] if self.controller_path else ['-m', 'doer_loop.cli']
        args = [self.executable, *controller, '--workspace', str(workspace),
                '--verify-file=' + relative, '--profile=' + c.profile,
                '--provider=' + c.provider, '--sol=' + c.model, '--luna=' + c.luna, '--execute']
        if 'expect_text' in body:
            args.append('--expect-text=' + body['expect_text'])
        args.extend(['--', body['task']])
        proc = None
        selector = selectors.DefaultSelector()
        output = bytearray()
        counts = {'out': 0, 'err': 0}
        status = None
        terminating = None
        next_scan = 0
        try:
            with self._lock:
                if self._cancelled.is_set():
                    return 'cancelled', None
                proc = subprocess.Popen(args, cwd=workspace, env=self._env(temporary),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
                self._pgid = proc.pid
            assert proc.stdout is not None and proc.stderr is not None
            for stream, label in ((proc.stdout, 'out'), (proc.stderr, 'err')):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            while True:
                now = time.monotonic()
                if self._cancelled.is_set():
                    status = 'cancelled'
                elif now >= deadline and status is None:
                    status = 'deadline'
                if status is None and now >= next_scan:
                    if not _within_budget(roots, self.max_workspace_files,
                                          self.max_workspace_bytes, deadline, self._cancelled):
                        status = 'cancelled' if self._cancelled.is_set() else 'failed'
                    next_scan = now + 0.02
                if terminating is None and (status is not None or proc.poll() is not None):
                    _signal_group(proc.pid, signal.SIGTERM)
                    terminating = time.monotonic()
                if terminating is not None and time.monotonic() - terminating >= self.term_grace:
                    # Do not use proc.poll() to decide: descendants may outlive it.
                    _signal_group(proc.pid, signal.SIGKILL)
                    if proc.poll() is not None and not selector.get_map():
                        break
                    if time.monotonic() - terminating >= self.term_grace + 0.5:
                        status = status or 'failed'
                        break
                for key, _ in selector.select(0.01):
                    block = os.read(key.fd, 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    counts[key.data] += len(block)
                    if counts[key.data] > self.max_output:
                        status = status or 'failed'
                    elif key.data == 'out':
                        output.extend(block)
        finally:
            if proc is not None:
                _signal_group(proc.pid, signal.SIGTERM)
                _signal_group(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                with self._lock:
                    self._pgid = None
                proc.stdout.close()
                proc.stderr.close()
            selector.close()
        if status is not None:
            return status, None
        if not _within_budget(roots, self.max_workspace_files, self.max_workspace_bytes,
                              deadline, self._cancelled):
            return 'failed', None
        result = _json_result(output)
        if not isinstance(result, dict):
            return 'failed', None
        if result.get('status') in ('needs_input', 'incomplete', 'error'):
            return result['status'], None
        if result.get('status') != 'verified' or proc.returncode != 0:
            return 'failed', None
        artifact = bounded_read(workspace / relative)
        if artifact is None or ('expect_text' in body and artifact.strip() != body['expect_text'].strip()):
            return 'failed', None
        return 'verified', artifact
