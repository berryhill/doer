"""Single-replica x402 v2 exact-EVM paid Doer service (trusted customer only)."""
import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import sqlite3
import threading
import time
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from worker import SubprocessRunner, bounded_read

MAX_BODY = 8192
MAX_PAYMENT = 8192
MAX_ARTIFACT = 32768
KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
ID_RE = re.compile(r"[0-9a-f]{32}\Z")
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\Z")
HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}\Z")
ACTIVE = ('verifying', 'settling', 'queued', 'running')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('non-finite number')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)


def safe_path(name):
    return (isinstance(name, str) and 1 <= len(name) <= 160 and
            all(p not in ('', '.', '..') for p in name.split('/')) and
            '\\' not in name and all(32 <= ord(c) < 127 for c in name))


def validate_body(raw):
    if not 0 < len(raw) <= MAX_BODY:
        raise ValueError('body limit')
    value = strict_json(raw)
    if (not isinstance(value, dict) or set(value) - {'task', 'verify_file', 'expect_text'} or
            not {'task', 'verify_file'} <= set(value)):
        raise ValueError('fields')
    if (not isinstance(value['task'], str) or not 1 <= len(value['task'].strip()) <= 4000 or
            '\x00' in value['task'] or not safe_path(value['verify_file'])):
        raise ValueError('task/path')
    if 'expect_text' in value and (not isinstance(value['expect_text'], str) or
                                  len(value['expect_text'].encode('utf-8')) > MAX_ARTIFACT):
        raise ValueError('expected text')
    canonical(value)  # reject unpaired surrogate escapes before any mutation
    return value


@dataclass(frozen=True)
class Config:
    db: Path
    workspaces: Path
    origin: str
    facilitator: str
    network: str
    asset: str
    pay_to: str
    amount: str
    profile: str
    provider: str
    model: str
    luna: str
    timeout: int = 1300
    capacity: int = 4
    max_pending: int = 64
    hermes_home: Path | None = None
    requirements_extra: dict | None = None
    max_records: int = 10000

    def __post_init__(self):
        origin, facilitator = urlsplit(self.origin), urlsplit(self.facilitator)
        for url in (origin, facilitator):
            if (url.scheme != 'https' or not url.hostname or url.username or url.password or
                    url.query or url.fragment):
                raise ValueError('HTTPS URLs required')
            _ = url.port
        if origin.path not in ('', '/'):
            raise ValueError('public origin must not have a path')
        if not re.fullmatch(r'eip155:[1-9][0-9]*', self.network):
            raise ValueError('EVM exact only')
        if any(not ADDRESS_RE.fullmatch(v or '') or int(v[2:], 16) == 0
               for v in (self.asset, self.pay_to)):
            raise ValueError('nonzero EVM addresses required')
        if not re.fullmatch(r'[1-9][0-9]{0,18}', self.amount or ''):
            raise ValueError('positive atomic amount required')
        if not all(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}', v or '')
                   for v in (self.profile, self.provider, self.model, self.luna)):
            raise ValueError('operator runtime identity required')
        if self.profile != 'default':
            raise ValueError('service uses only its dedicated default profile')
        if not 1 <= self.timeout <= 3600 or not 1 <= self.capacity <= 32:
            raise ValueError('limits')
        if not 1 <= self.max_pending <= self.max_records <= 100000:
            raise ValueError('record limits')
        if self.hermes_home is None or not self.hermes_home.is_dir():
            raise ValueError('dedicated Hermes home required')
        extra = self.requirements_extra or {}
        if not isinstance(extra, dict) or set(extra) - {'name', 'version'}:
            raise ValueError('only EIP-712 name/version extras accepted')
        if any(not isinstance(v, str) or not 1 <= len(v) <= 64 for v in extra.values()):
            raise ValueError('invalid token domain')

    @property
    def requirement(self):
        return {'scheme': 'exact', 'network': self.network, 'amount': self.amount,
                'asset': self.asset, 'payTo': self.pay_to, 'maxTimeoutSeconds': 300,
                'extra': self.requirements_extra or {}}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('facilitator redirects forbidden')


class HTTPSFacilitator:
    def __init__(self, origin):
        self.origin = origin.rstrip('/')
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, operation, payment, requirement):
        if operation not in ('verify', 'settle'):
            raise ValueError('unsupported operation')
        req = urllib.request.Request(self.origin + '/' + operation,
            data=canonical({'x402Version': 2, 'paymentPayload': payment,
                            'paymentRequirements': requirement}),
            headers={'Content-Type': 'application/json'}, method='POST')
        with self.opener.open(req, timeout=15) as response:
            if response.status != 200 or response.headers.get_content_type() != 'application/json':
                raise ValueError('invalid facilitator response')
            raw = response.read(MAX_PAYMENT + 1)
            if len(raw) > MAX_PAYMENT:
                raise ValueError('facilitator response limit')
            return strict_json(raw)


def payment_nonce(challenge):
    return challenge['extensions']['doer-intent']['info']['nonce']


class Service:
    def __init__(self, config, facilitator=None, runner=None):
        self.config = config
        self.facilitator = facilitator or HTTPSFacilitator(config.facilitator)
        self.runner = runner or SubprocessRunner(config)
        config.db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        config.workspaces.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(config.db.parent, 0o700)
        os.chmod(config.workspaces, 0o700)
        self.process_lock = open(str(config.db) + '.lock', 'a')
        try:
            fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.process_lock.close()
            raise ValueError('another service owns this database') from None
        self.db = sqlite3.connect(config.db, check_same_thread=False, isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL, body_hash TEXT NOT NULL,
                body TEXT NOT NULL, challenge TEXT NOT NULL, token_hash TEXT NOT NULL,
                payment_hash TEXT UNIQUE, payment_identity TEXT UNIQUE,
                payment_payer TEXT, payment_tx TEXT, settled_amount TEXT, status TEXT NOT NULL,
                artifact TEXT, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS receipt_tx ON jobs(payment_tx) WHERE payment_tx IS NOT NULL;
        ''')
        self.db.execute("UPDATE jobs SET status='settlement_unknown',updated=? WHERE status='settling'", (time.time(),))
        self.db.execute("UPDATE jobs SET status='interrupted',updated=? WHERE status IN ('running','verifying')", (time.time(),))
        os.chmod(config.db, 0o600)
        for suffix in ('-wal', '-shm', '.lock'):
            p = Path(str(config.db) + suffix)
            if p.exists():
                os.chmod(p, 0o600)
        self.worker = threading.Thread(target=self._loop, daemon=False)
        self.worker.start()

    def txn(self, action):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                result = action(self.db)
                self.db.execute('COMMIT')
                return result
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def challenge(self, row):
        return json.loads(row['challenge'])

    @staticmethod
    def authenticated(row, token):
        return bool(token and TOKEN_RE.fullmatch(token) and
                    hmac.compare_digest(row['token_hash'], hashlib.sha256(token.encode()).hexdigest()))

    def submit(self, raw, key, signature=None, access_token=None):
        body = validate_body(raw)
        if not KEY_RE.fullmatch(key or '') or not TOKEN_RE.fullmatch(access_token or ''):
            return 400, {'error': 'Idempotency-Key and client-generated X-Job-Token required'}, {}
        if self.stopping.is_set():
            return 503, {'error': 'shutting down'}, {}
        digest = hashlib.sha256(canonical(body)).hexdigest()
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        token_hash = hashlib.sha256(access_token.encode()).hexdigest()
        now = time.time()
        def reserve(db):
            row = db.execute('SELECT * FROM jobs WHERE key_hash=?', (key_hash,)).fetchone()
            if row:
                db.execute("UPDATE jobs SET status='expired',updated=? WHERE id=? AND status='unpaid' AND created<?",
                           (now, row['id'], now - 3600))
                return db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            db.execute("UPDATE jobs SET status='expired',updated=? WHERE status='unpaid' AND created<?", (now, now - 3600))
            if (db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= self.config.max_records or
                db.execute("SELECT count(*) FROM jobs WHERE status='unpaid'").fetchone()[0] >= self.config.max_pending):
                return None
            job_id = secrets.token_hex(16)
            resource = {'url': self.config.origin.rstrip('/') + '/doer',
                        'description': 'One bounded Doer execution', 'mimeType': 'application/json'}
            # Nonce is inside the EIP-3009 signed authorization, unlike resource/extra echoes.
            # Random job ID prevents predictable nonce collision; all immutable intent fields
            # including the retained result capability and exact payment requirement are bound.
            nonce = '0x' + hashlib.sha256(canonical(['doer-intent-v1', job_id, resource['url'],
                'POST', key_hash, digest, token_hash, self.config.requirement])).hexdigest()
            info = {'nonce': nonce, 'bodySha256': digest, 'tokenSha256': token_hash,
                    'method': 'POST', 'path': '/doer', 'idempotencyKeySha256': key_hash}
            challenge = {'x402Version': 2, 'resource': resource, 'accepts': [self.config.requirement],
                'extensions': {'doer-intent': {'info': info, 'schema': {'type': 'object',
                    'properties': {'nonce': {'type': 'string'}}, 'required': ['nonce']}}}}
            db.execute("INSERT INTO jobs (id,key_hash,body_hash,body,challenge,token_hash,status,created,updated) VALUES (?,?,?,?,?,?,'unpaid',?,?)",
                (job_id, key_hash, digest, canonical(body).decode(), canonical(challenge).decode(), token_hash, now, now))
            return db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        row = self.txn(reserve)
        if row is None:
            return 503, {'error': 'intent storage capacity exhausted'}, {'Retry-After': '30'}
        if not self.authenticated(row, access_token):
            return 409, {'error': 'idempotency key unavailable'}, {}
        if row['body_hash'] != digest:
            return 409, {'error': 'idempotency conflict'}, {}
        if row['status'] == 'expired':
            return 410, {'error': 'intent expired'}, {}
        if row['status'] != 'unpaid':
            return 202, self.public(row, access_token), self.receipt_headers(row)
        challenge = self.challenge(row)
        if (challenge['accepts'][0] != self.config.requirement or
                challenge['resource']['url'] != self.config.origin.rstrip('/') + '/doer'):
            return 409, {'error': 'unpaid intent configuration changed; no payment accepted'}, {}
        header = {'PAYMENT-REQUIRED': base64.b64encode(canonical(challenge)).decode()}
        if not signature:
            return 402, self.public(row), header
        try:
            if len(signature) > MAX_PAYMENT * 2:
                raise ValueError('payment limit')
            encoded = base64.b64decode(signature, validate=True)
            if len(encoded) > MAX_PAYMENT:
                raise ValueError('payment limit')
            payment = strict_json(encoded)
            if (not isinstance(payment, dict) or type(payment.get('x402Version')) is not int or
                    payment['x402Version'] != 2 or payment.get('accepted') != challenge['accepts'][0] or
                    payment.get('resource') != challenge['resource']):
                raise ValueError('requirements')
            auth = payment['payload']['authorization']
            if (not isinstance(auth, dict) or not ADDRESS_RE.fullmatch(auth.get('from', '')) or
                    auth.get('to', '').lower() != self.config.pay_to.lower() or auth.get('value') != self.config.amount or
                    not HASH_RE.fullmatch(auth.get('nonce', '')) or auth['nonce'].lower() != payment_nonce(challenge) or
                    not re.fullmatch(r'0x[0-9a-fA-F]{130}', payment['payload'].get('signature', ''))):
                raise ValueError('authorization')
            for name in ('validAfter', 'validBefore'):
                if not re.fullmatch(r'[0-9]{1,12}', auth.get(name, '')):
                    raise ValueError('time')
            if not int(auth['validAfter']) <= now < int(auth['validBefore']) <= now + 300:
                raise ValueError('authorization time window')
            identity = hashlib.sha256(canonical([self.config.network, self.config.asset.lower(),
                                                 auth['from'].lower(), auth['nonce'].lower()])).hexdigest()
            pay_hash = hashlib.sha256(canonical(payment)).hexdigest()
        except (ValueError, UnicodeError, TypeError, KeyError, AttributeError, RecursionError):
            return 400, {'error': 'invalid payment or intent nonce'}, {}
        def claim(db):
            existing = db.execute('SELECT id FROM jobs WHERE payment_identity=? OR payment_hash=?', (identity, pay_hash)).fetchone()
            if existing and existing['id'] != row['id']:
                return 'conflict'
            current = db.execute('SELECT status FROM jobs WHERE id=?', (row['id'],)).fetchone()[0]
            if current != 'unpaid':
                return 'processing'
            if db.execute("SELECT count(*) FROM jobs WHERE status IN ('verifying','settling','queued','running')").fetchone()[0] >= self.config.capacity:
                return 'full'
            db.execute("UPDATE jobs SET status='verifying',payment_hash=?,payment_identity=?,payment_payer=?,updated=? WHERE id=?",
                       (pay_hash, identity, auth['from'].lower(), time.time(), row['id']))
            return 'claimed'
        claim_result = self.txn(claim)
        if claim_result == 'full':
            return 503, {'error': 'execution capacity exhausted'}, {'Retry-After': '30'}
        if claim_result != 'claimed':
            return 409, {'error': 'payment already claimed or processing'}, {}
        def state(status):
            self.txn(lambda db: db.execute('UPDATE jobs SET status=?,updated=? WHERE id=?', (status, time.time(), row['id'])))
        try:
            verified = self.facilitator.call('verify', payment, challenge['accepts'][0])
            if (not isinstance(verified, dict) or verified.get('isValid') is not True or verified.get('invalidReason') or
                    not isinstance(verified.get('payer'), str) or verified['payer'].lower() != auth['from'].lower()):
                state('payment_failed')
                return 402, {'error': 'payment verification failed', 'id': row['id']}, header
        except Exception:
            state('payment_failed')
            return 502, {'error': 'payment verification unavailable', 'id': row['id']}, {}
        state('settling')  # durable before the external effect
        try:
            settled = self.facilitator.call('settle', payment, challenge['accepts'][0])
        except Exception:
            settled = None
        if isinstance(settled, dict) and not isinstance(settled.get('transaction'), str):
            settled = None
        if (not isinstance(settled, dict) or settled.get('success') is not True or settled.get('errorReason') or
                settled.get('network') != self.config.network or not HASH_RE.fullmatch(settled.get('transaction', '') or '') or
                not isinstance(settled.get('payer'), str) or settled['payer'].lower() != auth['from'].lower() or
                ('amount' in settled and settled['amount'] != self.config.amount)):
            state('settlement_unknown')
            return 502, {'error': 'settlement outcome unknown; reconcile without repayment', 'id': row['id']}, {}
        def admit(db):
            db.execute("UPDATE jobs SET status='queued',payment_tx=?,settled_amount=?,updated=? WHERE id=? AND status='settling'",
                       (settled['transaction'].lower(), self.config.amount, time.time(), row['id']))
            return db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
        try:
            result = self.txn(admit)
        except sqlite3.IntegrityError:
            state('settlement_unknown')
            return 502, {'error': 'settlement receipt conflict', 'id': row['id']}, {}
        self.wake.set()
        return 202, self.public(result, access_token), self.receipt_headers(result)

    def receipt_headers(self, row):
        if not row['payment_tx']:
            return {}
        req = self.challenge(row)['accepts'][0]
        receipt = {'success': True, 'network': req['network'], 'transaction': row['payment_tx'],
                   'payer': row['payment_payer'], 'amount': row['settled_amount']}
        return {'PAYMENT-RESPONSE': base64.b64encode(canonical(receipt)).decode()}

    def public(self, row, token=None):
        value = {'id': row['id'], 'status': row['status'], 'status_url': '/jobs/' + row['id'],
                 'result_url': '/jobs/' + row['id'] + '/result'}
        if token:
            value['access_token'] = token
        if row['payment_identity']:
            req = self.challenge(row)['accepts'][0]
            value['payment'] = {'network': req['network'], 'asset': req['asset'], 'pay_to': req['payTo'],
                'amount': req['amount'], 'settled_amount': row['settled_amount'],
                'transaction': row['payment_tx'], 'payer': row['payment_payer']}
        return value

    def read_job(self, job_id, token, result=False):
        if not ID_RE.fullmatch(job_id or ''):
            return 404, {'error': 'not found'}
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None or not self.authenticated(row, token):
            return 404, {'error': 'not found'}
        if result:
            return ((200, {'text': row['artifact']}) if row['status'] == 'verified' else
                    (409, {'error': 'result unavailable', 'status': row['status']}))
        return 200, self.public(row)

    def _loop(self):
        while not self.stopping.is_set():
            def claim(db):
                row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
                return row
            row = self.txn(claim)
            if not row:
                self.wake.wait(0.5)
                self.wake.clear()
                continue
            status, artifact = 'failed', None
            try:
                workspace = self.config.workspaces / row['id']
                workspace.mkdir(mode=0o700)
                status, artifact = self.runner(row['id'], json.loads(row['body']), workspace)
                if status not in ('verified', 'needs_input', 'incomplete', 'error', 'deadline', 'cancelled', 'failed'):
                    status, artifact = 'failed', None
                if status == 'verified' and (not isinstance(artifact, str) or len(artifact.encode()) > MAX_ARTIFACT):
                    status, artifact = 'failed', None
            except Exception:
                status, artifact = 'failed', None
            self.txn(lambda db: db.execute("UPDATE jobs SET status=?,artifact=?,updated=? WHERE id=? AND status='running'",
                     (status, artifact if status == 'verified' else None, time.time(), row['id'])))

    def close(self):
        self.stopping.set()
        if hasattr(self.runner, 'cancel'):
            self.runner.cancel()
        self.wake.set()
        self.worker.join(timeout=10)
        if self.worker.is_alive():
            raise RuntimeError('worker did not stop; retain database ownership')
        with self.lock:
            self.db.close()
            self.process_lock.close()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def reply(self, status, data, headers=None):
        raw = canonical(data)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(raw)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path != '/doer':
            return self.reply(404, {'error': 'not found'})
        for name in ('Content-Length', 'Content-Type', 'Idempotency-Key', 'X-Job-Token', 'PAYMENT-SIGNATURE'):
            if len(self.headers.get_all(name, [])) > 1:
                return self.reply(400, {'error': 'duplicate header'})
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            return self.reply(400, {'error': 'JSON with Content-Length required'})
        try:
            length = int(self.headers.get('Content-Length', ''))
            if not 0 < length <= MAX_BODY:
                return self.reply(413, {'error': 'body size'})
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.reply(400, {'error': 'incomplete body'})
            status, data, headers = self.server.service.submit(raw, self.headers.get('Idempotency-Key'),
                self.headers.get('PAYMENT-SIGNATURE'), self.headers.get('X-Job-Token'))
            self.reply(status, data, headers)
        except (ValueError, UnicodeError, TypeError, RecursionError):
            self.reply(400, {'error': 'invalid request'})
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            self.reply(500, {'error': 'internal error'})

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'status': 'alive'})
        if self.path == '/ready':
            try:
                with self.server.service.lock:
                    self.server.service.db.execute('SELECT 1').fetchone()
                ready = not self.server.service.stopping.is_set() and self.server.service.worker.is_alive()
                return self.reply(200 if ready else 503, {'ready': ready, 'scope': 'controller; not model or settlement proof'})
            except sqlite3.Error:
                return self.reply(503, {'ready': False})
        match = re.fullmatch(r'/jobs/([0-9a-f]{32})(/result)?', self.path)
        if not match or len(self.headers.get_all('X-Job-Token', [])) != 1:
            return self.reply(404, {'error': 'not found'})
        self.reply(*self.server.service.read_job(match[1], self.headers.get('X-Job-Token'), bool(match[2])))


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 32
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)
    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise
    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def serve(service, host='127.0.0.1', port=8080):
    server = BoundedServer((host, port), Handler)
    server.service = service
    return server


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8080')))
    args = parser.parse_args()
    names = ('DOER_ORIGIN', 'DOER_FACILITATOR', 'DOER_NETWORK', 'DOER_ASSET', 'DOER_PAY_TO',
             'DOER_AMOUNT', 'DOER_PROFILE', 'DOER_PROVIDER', 'DOER_MODEL', 'DOER_LUNA')
    if any(not os.environ.get(n) for n in names):
        parser.error('missing required DOER configuration')
    try:
        config = Config(Path(os.environ.get('DOER_DB', '/data/doer.sqlite')),
            Path(os.environ.get('DOER_WORKSPACES', '/data/workspaces')), *(os.environ[n] for n in names),
            hermes_home=Path(os.environ.get('DOER_HERMES_HOME', '/runtime/hermes')),
            requirements_extra=json.loads(os.environ.get('DOER_PAYMENT_EXTRA', '{}')),
            timeout=int(os.environ.get('DOER_JOB_TIMEOUT', '1300')),
            capacity=int(os.environ.get('DOER_CAPACITY', '4')))
    except (ValueError, TypeError):
        parser.error('invalid service configuration')
    service = Service(config)
    server = serve(service, args.host, args.port)
    def shutdown(*_):
        service.stopping.set()
        if hasattr(service.runner, 'cancel'):
            service.runner.cancel()
        threading.Thread(target=server.shutdown, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()  # finish admission handlers before closing SQLite
        service.close()


if __name__ == '__main__':
    main()
