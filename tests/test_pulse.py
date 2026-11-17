import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import pulse  # noqa: E402

ENV = {'SELF_REPO': 'owner/runner', 'DISPATCH_TOKEN': 't', 'SELF_REF': 'main'}


class FakeApi:
    def __init__(self, active=None, fail=False, post_failures=0):
        self.active = active or {}
        self.fail = fail
        self.post_failures = post_failures
        self.calls = []

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, body))
        if self.fail:
            raise pulse.ApiError('500')
        if method == 'POST' and self.post_failures:
            self.post_failures -= 1
            raise pulse.ApiError('502')
        if method == 'GET':
            status = path.split('status=')[1].split('&')[0]
            return {'total_count': self.active.get(status, 0)}
        return None


def run(argv, fake, env=ENV):
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(pulse, 'api', fake), mock.patch.object(pulse.time, 'sleep'), \
            redirect_stdout(out):
        code = pulse.main(['pulse.py', *argv])
    return code, out.getvalue().strip()


def dispatches(fake):
    return [c for c in fake.calls if c[0] == 'POST']


class PulseTests(unittest.TestCase):
    def test_next_always_dispatches_on_the_ref(self):
        fake = FakeApi(active={'in_progress': 1})
        self.assertEqual(run(['next'], fake), (0, 'pulse: dispatched'))
        self.assertEqual(dispatches(fake), [('POST', '/repos/owner/runner/actions/workflows/pulse.yml/dispatches', {'ref': 'main'})])

    def test_ensure_leaves_a_live_chain_alone(self):
        for status in pulse.ACTIVE:
            fake = FakeApi(active={status: 1})
            self.assertEqual(run(['ensure'], fake), (0, 'pulse: running'), status)
            self.assertEqual(dispatches(fake), [], status)

    def test_ensure_restarts_a_broken_chain(self):
        fake = FakeApi()
        self.assertEqual(run(['ensure'], fake), (0, 'pulse: dispatched'))
        self.assertEqual(len(dispatches(fake)), 1)

    def test_a_transient_dispatch_failure_is_retried(self):
        fake = FakeApi(post_failures=len(pulse.RETRY_SECONDS))
        self.assertEqual(run(['next'], fake), (0, 'pulse: dispatched'))
        self.assertEqual(len(dispatches(fake)), len(pulse.RETRY_SECONDS) + 1)

    def test_a_dispatch_that_keeps_failing_gives_up(self):
        fake = FakeApi(post_failures=len(pulse.RETRY_SECONDS) + 1)
        self.assertEqual(run(['next'], fake), (1, 'pulse: dispatch failed (502)'))
        self.assertEqual(len(dispatches(fake)), len(pulse.RETRY_SECONDS) + 1)

    def test_failures_and_bad_configuration_exit_nonzero(self):
        self.assertEqual(run(['ensure'], FakeApi(fail=True)), (1, 'pulse: dispatch failed (500)'))
        self.assertEqual(run(['other'], FakeApi())[0], 1)
        self.assertEqual(run(['next'], FakeApi(), env={'SELF_REPO': 'not a repo', 'DISPATCH_TOKEN': 't'}), (1, 'pulse: not configured'))
        self.assertEqual(run(['next'], FakeApi(), env={'SELF_REPO': 'owner/runner'}), (1, 'pulse: not configured'))


if __name__ == '__main__':
    unittest.main()
