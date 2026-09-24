"""Real loopback HTTP and synthetic x402 facilitator tests; never moves money."""
import base64
import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from service import (Config, Service, canonical, serve, bounded_read,
                     payment_nonce, HTTPSFacilitator, NoRedirect)

ADDR = '0x' + '1' * 40
ASSET = '0x' + '2' * 40
KEY = 'key_0123456789abcd'
TOKEN = 'synthetic_client_capability_' + 'a' * 32
BODY = {'task': 'Create done', 'verify_file': 'out.txt'}


class Facilitator:
    def __init__(self):
        self.calls = []
        self.verify_response = {'isValid': True, 'payer': ADDR}
        self.settle_response = {'success': True, 'payer': ADDR, 'transaction': '0x' + '3' * 64, 'network': 'eip155:84532'}
        self.settle_error = None

    def call(self, operation, payment, requirement):
        self.calls.append((operation, payment, requirement))
        if operation == 'settle' and self.settle_error:
            raise self.settle_error
        return self.verify_response if operation == 'verify' else self.settle_response


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.facilitator, self.runs = Facilitator(), []
        def runner(job_id, body, workspace):
            self.runs.append(job_id)
            path = workspace / body['verify_file']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('done')
            return 'verified', bounded_read(path)
        self.runner = runner
        self.config = Config(root / 'state/db.sqlite', root / 'jobs', 'https://doer.example',
            'https://facilitator.example', 'eip155:84532', ASSET, ADDR, '100',
            'default', 'openai-codex', 'gpt-6-sol', 'gpt-6-luna', hermes_home=root)
        self.start()

    def start(self):
        self.service = Service(self.config, self.facilitator, self.runner)
        self.server = serve(self.service, '127.0.0.1', 0)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()

    def tearDown(self):
        self.stop()
        self.temp.cleanup()

    def request(self, method='POST', path='/doer', data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        body = data if isinstance(data, bytes) else canonical(BODY if data is None else data)
        h = {'Content-Type': 'application/json', 'Idempotency-Key': KEY}
        if method == 'POST':
            h['X-Job-Token'] = TOKEN
        h.update(headers or {})
        conn.request(method, path, body if method == 'POST' else None, h)
        response = conn.getresponse()
        result = response.status, json.loads(response.read()), dict(response.getheaders())
        conn.close()
        return result

    def challenge(self, key=KEY, body=None):
        status, job, headers = self.request(data=body, headers={'Idempotency-Key': key})
        self.assertEqual(status, 402)
        return json.loads(base64.b64decode(headers['PAYMENT-REQUIRED']))

    def signature(self, challenge, nonce=None):
        payment = {'x402Version': 2, 'resource': challenge['resource'],
            'accepted': challenge['accepts'][0], 'extensions': challenge['extensions'],
            'payload': {'signature': '0x' + '4' * 130, 'authorization': {
                'from': ADDR, 'to': ADDR, 'value': '100', 'nonce': nonce or payment_nonce(challenge),
                'validAfter': str(int(time.time()) - 1), 'validBefore': str(int(time.time()) + 120)}}}
        return base64.b64encode(canonical(payment)).decode()

    def paid(self, challenge=None, key=KEY, nonce=None, body=None):
        return self.request(data=body, headers={'Idempotency-Key': key,
            'PAYMENT-SIGNATURE': self.signature(challenge or self.challenge(key, body), nonce)})

    def status(self, job):
        for _ in range(200):
            code, data, _ = self.request('GET', job['status_url'], headers={'X-Job-Token': TOKEN})
            self.assertEqual(code, 200)
            if data['status'] not in ('queued', 'running'):
                return data
            time.sleep(0.01)
        self.fail('worker did not finish')

    def test_paid_job_private_and_replay_survives_lost_response(self):
        challenge = self.challenge()
        self.assertEqual(challenge, self.challenge())
        self.assertFalse(self.runs)
        self.assertEqual(self.paid(challenge)[0], 202)  # discard the complete response
        status, job, headers = self.request()
        self.assertEqual(status, 202)
        self.assertIn('PAYMENT-RESPONSE', headers)
        self.assertEqual(self.status(job)['status'], 'verified')
        self.assertEqual(self.request('GET', job['result_url'], headers={'X-Job-Token': TOKEN})[:2], (200, {'text': 'done'}))
        self.assertEqual(self.request('GET', job['status_url'])[0], 404)
        self.assertEqual(self.request('GET', job['status_url'], headers={'X-Job-Token': 'wrong'})[0], 404)
        self.assertEqual(self.request(headers={'X-Job-Token': 'b' * 43})[0], 409)
        self.assertEqual([x[0] for x in self.facilitator.calls], ['verify', 'settle'])
        self.assertEqual(len(self.runs), 1)
        self.stop()
        self.start()
        self.assertEqual(self.request()[1]['id'], job['id'])
        self.assertEqual(len(self.runs), 1)

    def test_malformed_requests_fail_before_payment(self):
        for value in ({'task': 'x', 'verify_file': '../x'}, {'task': 'x', 'verify_file': '/x'},
                      {'task': 'x', 'verify_file': 'x', 'profile': 'other'},
                      b'{"task":"x","task":"y","verify_file":"x"}', b'\xff', b'x' * 8193,
                      b'{"task":"\\ud800","verify_file":"x"}'):
            self.assertIn(self.request(data=value)[0], (400, 413))
        self.assertEqual(self.request(headers={'X-Job-Token': ''})[0], 400)
        self.assertEqual(self.request(headers={'PAYMENT-SIGNATURE': '!!!'})[0], 400)
        self.assertFalse(self.facilitator.calls)
        self.assertFalse(self.runs)

    def test_body_conflict_and_signed_nonce_substitution(self):
        first = self.challenge()
        other = {'task': 'another task', 'verify_file': 'out.txt'}
        self.assertEqual(self.request(data=other)[0], 409)
        key = 'another_key_0123456789'
        second = self.challenge(key, other)
        self.assertNotEqual(payment_nonce(first), payment_nonce(second))
        self.assertEqual(self.paid(second, key, nonce=payment_nonce(first), body=other)[0], 400)
        self.assertFalse(self.facilitator.calls)
        self.assertEqual(self.paid(first)[0], 202)
        self.assertEqual(self.paid(second, key, nonce=payment_nonce(first), body=other)[0], 400)
        self.assertEqual(len(self.facilitator.calls), 2)

    def test_verify_payer_mismatch_and_invalid_do_not_settle(self):
        self.facilitator.verify_response = {'isValid': True, 'payer': '0x' + '9' * 40}
        self.assertEqual(self.paid()[0], 402)
        self.assertEqual([x[0] for x in self.facilitator.calls], ['verify'])
        self.assertFalse(self.runs)

    def test_invalid_verify_fails_closed(self):
        self.facilitator.verify_response = {'isValid': 'true'}
        self.assertEqual(self.paid()[0], 402)
        self.assertFalse(self.runs)

    def test_empty_settlement_transaction_is_unknown(self):
        self.facilitator.settle_response['transaction'] = ''
        self.assertEqual(self.paid()[0], 502)
        self.assertEqual(self.request()[1]['status'], 'settlement_unknown')
        self.assertFalse(self.runs)

    def test_mismatched_settlement_amount_is_unknown(self):
        self.facilitator.settle_response['amount'] = '99'
        self.assertEqual(self.paid()[0], 502)
        self.assertFalse(self.runs)

    def test_settlement_timeout_restart_never_recharges(self):
        self.facilitator.settle_error = TimeoutError()
        c = self.challenge()
        self.assertEqual(self.paid(c)[0], 502)
        self.assertEqual(self.request()[1]['status'], 'settlement_unknown')
        self.stop()
        self.start()
        self.assertEqual(self.paid(c)[1]['status'], 'settlement_unknown')
        self.assertEqual(len(self.facilitator.calls), 2)
        self.assertFalse(self.runs)

    def test_recovery_running_and_settling(self):
        self.challenge()
        self.stop()
        with sqlite3.connect(self.config.db) as db:
            db.execute("UPDATE jobs SET status='running'")
        self.start()
        self.assertEqual(self.request()[1]['status'], 'interrupted')
        self.stop()
        with sqlite3.connect(self.config.db) as db:
            db.execute("UPDATE jobs SET status='settling'")
        self.start()
        self.assertEqual(self.request()[1]['status'], 'settlement_unknown')
        self.assertFalse(self.runs)

    def test_existing_unpaid_challenge_capacity_checked_atomically(self):
        c = self.challenge()
        self.service.config = replace(self.config, capacity=1)
        self.service.txn(lambda db: db.execute("INSERT INTO jobs (id,key_hash,body_hash,body,challenge,token_hash,status,created,updated) VALUES ('other','other','other','{}','{}','x','running',0,0)"))
        self.assertEqual(self.paid(c)[0], 503)
        self.assertFalse(self.facilitator.calls)
        self.assertEqual(self.request()[0], 402)

    def test_concurrent_replays_one_settlement(self):
        c = self.challenge()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.paid(c)[0])) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertIn(202, results)
        self.assertTrue(all(s in (202, 409) for s in results))
        self.assertEqual([x[0] for x in self.facilitator.calls], ['verify', 'settle'])

    def test_worker_failure_preserves_receipt(self):
        self.service.runner = lambda *_: ('failed', None)
        _, job, _ = self.paid()
        view = self.status(job)
        self.assertEqual(view['status'], 'failed')
        self.assertEqual(view['payment']['transaction'], self.facilitator.settle_response['transaction'])
        self.assertEqual(self.request('GET', job['result_url'], headers={'X-Job-Token': TOKEN})[0], 409)

    def test_second_service_cannot_reset_live_jobs(self):
        with self.assertRaises(ValueError):
            Service(self.config, self.facilitator, self.runner)
        self.assertEqual(self.request('GET', '/ready')[0], 200)

    def test_payment_metadata_stays_pinned_across_config_change(self):
        _, job, _ = self.paid()
        self.status(job)
        self.service.config = replace(self.config, amount='200')
        self.assertEqual(self.request()[1]['payment']['amount'], '100')

    def test_expiration_and_record_bound(self):
        self.challenge()
        self.service.txn(lambda db: db.execute('UPDATE jobs SET created=0'))
        self.assertEqual(self.request()[0], 410)
        self.service.config = replace(self.config, max_pending=1, max_records=1)
        self.assertEqual(self.request(headers={'Idempotency-Key': 'new_key_0123456789'})[0], 503)

    def test_health_and_safe_artifact(self):
        self.assertEqual(self.request('GET', '/health')[0], 200)
        self.assertEqual(self.request('GET', '/ready')[0], 200)
        root = Path(self.temp.name)
        (root / 'artifact').write_text('private')
        (root / 'link').symlink_to(root / 'artifact')
        self.assertIsNone(bounded_read(root / 'link'))
        self.assertIsNone(bounded_read(root / 'artifact', limit=1))


class ProtocolAdapterTests(unittest.TestCase):
    def test_facilitator_wire_contract(self):
        from email.message import Message
        headers = Message()
        headers['Content-Type'] = 'application/json'
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, size): return b'{"isValid":true}'
        Response.headers = headers
        adapter = HTTPSFacilitator('https://facilitator.example/x402')
        with patch.object(adapter.opener, 'open', return_value=Response()) as call:
            self.assertEqual(adapter.call('verify', {'test': 'synthetic'}, {'scheme': 'exact'}), {'isValid': True})
            request = call.call_args.args[0]
            self.assertEqual(request.full_url, 'https://facilitator.example/x402/verify')
            self.assertEqual(json.loads(request.data), {'x402Version': 2,
                'paymentPayload': {'test': 'synthetic'}, 'paymentRequirements': {'scheme': 'exact'}})
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, 307, '', {}, 'https://evil.example')


if __name__ == '__main__':
    unittest.main()
