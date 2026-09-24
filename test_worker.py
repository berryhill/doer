"""Real synthetic subprocess coverage; never invoke a model or provider."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from worker import SubprocessRunner, bounded_read


@pytest.fixture
def root():
    scratch = Path.home() / '.hermes' / '.scratch'
    scratch.mkdir(parents=True, exist_ok=True)
    # Retain synthetic evidence; do not delete user-owned paths.
    return Path(tempfile.mkdtemp(prefix='worker-test-', dir=scratch))


def make(root, source, **kwargs):
    controller = root / 'synthetic.py'
    controller.write_text('import os, sys, json, time, signal, subprocess\nfrom pathlib import Path\n' + source)
    workspace = root / 'workspace'
    workspace.mkdir()
    cfg = SimpleNamespace(timeout=kwargs.pop('timeout', 2), hermes_home=root / 'runtime',
                          profile='default', provider='test', model='sol', luna='luna')
    return SubprocessRunner(cfg, controller_path=controller, **kwargs), workspace


def run(runner, workspace, **body):
    return runner('job', {'task': '-leading prompt', 'verify_file': 'result.txt', **body}, workspace)


@pytest.mark.parametrize('fd', [1, 2])
def test_flood_terminated_without_logs(root, fd):
    r, w = make(root, f"while True: os.write({fd}, b'x' * 8192)\n", max_output=16384)
    start = time.monotonic()
    assert run(r, w) == ('failed', None)
    assert time.monotonic() - start < 2
    assert not list(w.iterdir())


def test_verified_real_artifact_and_dash_prompt(root):
    r, w = make(root, "assert sys.argv[-2:] == ['--', '-leading prompt']\n"
                     "Path('result.txt').write_text('hello')\nprint(json.dumps({'status':'verified'}))\n")
    assert run(r, w, expect_text='hello') == ('verified', 'hello')


@pytest.mark.parametrize('source', [
    "pass", "Path('result.txt').symlink_to('/etc/passwd')",
    "Path('result.txt').write_bytes(b'x' * 32769)", "os.mkfifo('result.txt')",
    "Path('other').write_text('secret'); os.link('other', 'result.txt')",
])
def test_verified_requires_safe_artifact(root, source):
    r, w = make(root, source + "\nprint(json.dumps({'status':'verified'}))\n")
    assert run(r, w) == ('failed', None)


@pytest.mark.parametrize('status', ['needs_input', 'incomplete', 'error'])
def test_nonverified_status_preserved_without_raw_errors(root, status):
    r, w = make(root, f"print(json.dumps({{'status':{status!r},'error':'SECRET'}}))\nsys.exit(1)\n")
    assert run(r, w) == (status, None)


def test_invalid_output_sanitized(root):
    r, w = make(root, "print('SECRET BAD OUTPUT')\n")
    assert run(r, w) == ('failed', None)


def test_environment_is_allowlisted_and_tmp_unique(root, monkeypatch):
    monkeypatch.setenv('PAYMENT_SIGNATURE', 'secret')
    monkeypatch.setenv('DOER_PAY_TO', 'secret')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'secret')
    monkeypatch.setenv('DOER_LAYA_MODEL_DIR', '/pinned/weights')
    r, w = make(root, "Path('result.txt').write_text(json.dumps(dict(os.environ)))\n"
                     "print(json.dumps({'status':'verified'}))\n")
    status, artifact = run(r, w)
    assert status == 'verified'
    env = json.loads(artifact)
    for key in ('PAYMENT_SIGNATURE', 'DOER_PAY_TO', 'AWS_SECRET_ACCESS_KEY'):
        assert key not in env
    assert env['PATH'] == '/usr/local/bin:/usr/bin:/bin'
    assert env['HERMES_HOME'] == str(root / 'runtime')
    assert env['DOER_LAYA_MODEL_DIR'] == '/pinned/weights'
    assert env['HF_HUB_OFFLINE'] == env['TRANSFORMERS_OFFLINE'] == '1'
    assert env['OMP_NUM_THREADS'] == env['TORCH_NUM_THREADS'] == '1'
    tmp = Path(env['TMPDIR'])
    assert tmp.is_dir() and tmp.parent == Path.home() / '.hermes' / '.scratch'
    assert tmp != w
    assert r._env(tmp)['TMPDIR'] == str(tmp)
    monkeypatch.delenv('DOER_LAYA_MODEL_DIR')
    assert 'DOER_LAYA_MODEL_DIR' not in r._env(tmp)
    assert 'HF_HUB_OFFLINE' not in r._env(tmp)


def live(pid):
    try:
        # A killed orphan can remain a zombie until init reaps it.
        return Path(f'/proc/{pid}/stat').read_text().split(') ')[1][0] != 'Z'
    except FileNotFoundError:
        return False


def wait_file(path):
    deadline = time.monotonic() + 2
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert path.exists()


@pytest.mark.parametrize('parent_exit', [False, True])
@pytest.mark.parametrize('closed_pipes', [False, True])
def test_timeout_or_parent_exit_kills_ignoring_descendant(root, parent_exit, closed_pipes):
    stdio = ', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL' if closed_pipes else ''
    source = "child = subprocess.Popen([sys.executable, '-c', \"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\"]" + stdio + ")\n"
    source += "Path('pid').write_text(str(child.pid))\ntime.sleep(.1)\n"
    if parent_exit:
        source += "print(json.dumps({'status':'incomplete'}))\n"
    else:
        source += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)\n"
    r, w = make(root, source, timeout=.5)
    status, artifact = run(r, w)
    assert status == ('incomplete' if parent_exit else 'deadline')
    assert artifact is None
    pid = int((w / 'pid').read_text())
    end = time.monotonic() + 1
    while live(pid) and time.monotonic() < end:
        time.sleep(.01)
    assert not live(pid)


def test_cancellation_kills_active_group(root):
    r, w = make(root, "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                     "Path('pid').write_text(str(os.getpid()))\ntime.sleep(30)\n", timeout=30)
    result = []
    thread = threading.Thread(target=lambda: result.append(run(r, w)))
    thread.start()
    wait_file(w / 'pid')
    start = time.monotonic()
    r.cancel()
    thread.join(2)
    assert not thread.is_alive()
    assert result == [('cancelled', None)]
    assert time.monotonic() - start < 1
    assert not live(int((w / 'pid').read_text()))
    assert run(r, w) == ('cancelled', None)


@pytest.mark.parametrize('kind', ['bytes', 'files', 'tmp'])
def test_workspace_growth_monitor(root, kind):
    if kind == 'bytes':
        source = "f = open('big', 'wb')\nwhile True:\n f.write(b'x'*8192); f.flush(); time.sleep(.001)\n"
    elif kind == 'tmp':
        source = "f = open(Path(os.environ['TMPDIR']) / 'big', 'wb')\nwhile True:\n f.write(b'x'*8192); f.flush(); time.sleep(.001)\n"
    else:
        source = "for n in range(10000):\n Path(str(n)).touch(); time.sleep(.001)\n"
    r, w = make(root, source, max_workspace_files=8, max_workspace_bytes=32768)
    assert run(r, w) == ('failed', None)
    assert w.is_dir()
    if kind == 'bytes':
        assert (w / 'big').stat().st_size < 4 * 1024 * 1024
    if kind == 'files':
        assert len(list(w.iterdir())) < 1000


def test_bounded_read_nofollow_ancestors_fifo_hardlink(root):
    good = root / 'good'
    good.mkdir()
    p = good / 'result'
    p.write_text('valid')
    assert bounded_read(p) == 'valid'
    assert bounded_read(p, 2) is None
    link = root / 'link'
    link.symlink_to(good, target_is_directory=True)
    assert bounded_read(link / 'result') is None
    fifo = root / 'fifo'
    os.mkfifo(fifo)
    assert bounded_read(fifo) is None
    os.link(p, root / 'hardlink')
    assert bounded_read(p) is None


def test_forged_verified_with_failed_exit(root):
    r, w = make(root, "Path('result.txt').write_text('hello')\n"
                     "print(json.dumps({'status':'verified'}))\nsys.exit(1)\n")
    assert run(r, w) == ('failed', None)
