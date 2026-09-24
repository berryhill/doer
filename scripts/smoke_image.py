"""Offline image acceptance: real HTTP unpaid boundary and runtime imports.

Run: python scripts/smoke_image.py IMAGE
Creates and retains a stopped synthetic container for inspection; no money/models.
"""
import json
import subprocess
import sys
import uuid

image = sys.argv[1]
name = 'doer-smoke-' + uuid.uuid4().hex[:12]
values = {
    'DOER_ORIGIN': 'https://doer.example', 'DOER_FACILITATOR': 'https://facilitator.example',
    'DOER_NETWORK': 'eip155:84532', 'DOER_ASSET': '0x' + '2' * 40,
    'DOER_PAY_TO': '0x' + '1' * 40, 'DOER_AMOUNT': '100', 'DOER_PROFILE': 'default',
    'DOER_PROVIDER': 'openai-codex', 'DOER_MODEL': 'gpt-6-sol', 'DOER_LUNA': 'gpt-6-luna',
}
command = ['docker', 'run', '-d', '--name', name, '--network', 'none', '--cap-drop', 'ALL',
           '--security-opt', 'no-new-privileges', '--memory', '1g', '--cpus', '1', '--pids-limit', '128']
for key, value in values.items():
    command.extend(['-e', key + '=' + value])
subprocess.run(command + [image], check=True, capture_output=True)
try:
    def execute(*args):
        return subprocess.run(['docker', 'exec', name, *args], capture_output=True, text=True, check=True, timeout=90).stdout
    help_text = execute('/usr/local/bin/hermes', 'chat', '--help')
    assert all(flag in help_text for flag in ('--query-file', '--format', '--source', '--in', '--max-turns', '--run-budget'))
    assert '--verify-file' in execute('/opt/laya-venv/bin/python', '-m', 'doer_loop.cli', '--help')
    assert '--verify-file' in execute('/opt/laya-venv/bin/doer', '--help')
    print('installed Doer module and console entrypoint: PASS')
    print(execute('/opt/laya-venv/bin/python', '-c', "import os,torch; from laya import Router; assert os.getuid()==10001; assert torch.version.cuda is None; print('non-root + CPU Laya imports: PASS')").strip())
    print(execute('/opt/laya-venv/bin/python', '-c', '''
import base64,json,time,urllib.request,urllib.error
base='http://127.0.0.1:8080'
for attempt in range(30):
    try:
        assert json.load(urllib.request.urlopen(base+'/health',timeout=2))['status']=='alive'
        break
    except OSError:
        if attempt==29: raise
        time.sleep(.1)
assert json.load(urllib.request.urlopen(base+'/ready',timeout=2))['ready'] is True
request=urllib.request.Request(base+'/doer',data=json.dumps({'task':'Create hello','verify_file':'hello.txt'}).encode(),headers={'Content-Type':'application/json','Idempotency-Key':'synthetic_0123456789','X-Job-Token':'synthetic_'+'x'*43})
try:
    urllib.request.urlopen(request,timeout=3)
    raise AssertionError('unpaid request was accepted')
except urllib.error.HTTPError as error:
    assert error.code==402
    challenge=json.loads(base64.b64decode(error.headers['PAYMENT-REQUIRED']))
    assert challenge['x402Version']==2
    assert len(challenge['extensions']['doer-intent']['info']['nonce'])==66
    assert json.load(error)['status']=='unpaid'
print('real image /health + /ready + unpaid HTTP 402: PASS')
''').strip())
finally:
    stopped = subprocess.run(['docker', 'stop', '--time', '40', name], capture_output=True, text=True, timeout=50)
    if stopped.returncode:
        raise RuntimeError('container shutdown failed: ' + name)
state = json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .State}}', name], text=True))
assert state['ExitCode'] == 0 and not state['OOMKilled'], state
print('Hermes CLI flags + graceful SIGTERM: PASS')
print('Retained stopped container:', name)
