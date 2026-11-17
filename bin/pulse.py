#!/usr/bin/env python3
"""Keep the poller alive without GitHub's cron.

GitHub's schedule is best effort; in this repository it has not fired once since the repository was created (2026-11-17), so
the quality poller never ran and pull requests waited for a manual dispatch. `pulse` (.github/workflows/pulse.yml) is a chain
of workflow_dispatch runs instead: each run polls for a while and then dispatches the next one. A workflow_dispatch made with
the job token does start a run (the documented exception to the GITHUB_TOKEN rule).

  pulse.py next    dispatch the next pulse run (called by a pulse run at its end)
  pulse.py ensure  dispatch a pulse run only when none is queued, waiting or running (called by every quality run and by
                   pulse-watchdog after every pulse run, so a run that ends without a successor restarts the chain)

  DISPATCH_TOKEN, SELF_REPO, SELF_REF
Prints one fixed line.
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poll import ApiError, api  # noqa: E402

WORKFLOW = 'pulse.yml'
ACTIVE = ('in_progress', 'queued', 'pending', 'waiting', 'requested')
RETRY_SECONDS = (10, 30, 90)  # a transient API failure must not end the chain


def active_runs(repo: str, token: str) -> int:
    count = 0
    for status in ACTIVE:
        runs = api('GET', f'/repos/{repo}/actions/workflows/{WORKFLOW}/runs?status={status}&per_page=1', token)
        count += int((runs or {}).get('total_count') or 0)
    return count


def dispatch(repo: str, token: str, ref: str) -> None:
    for wait in (*RETRY_SECONDS, None):
        try:
            api('POST', f'/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches', token, {'ref': ref})
            return
        except ApiError:
            if wait is None:
                raise
            time.sleep(wait)


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ''
    repo, token, ref = os.environ.get('SELF_REPO', ''), os.environ.get('DISPATCH_TOKEN', ''), os.environ.get('SELF_REF', 'main')
    if mode not in ('next', 'ensure') or not (re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) and token):
        print('pulse: not configured')
        return 1
    try:
        if mode == 'ensure' and active_runs(repo, token) > 0:
            print('pulse: running')
            return 0
        dispatch(repo, token, ref)
    except ApiError as e:
        print(f'pulse: dispatch failed ({e})')
        return 1
    print('pulse: dispatched')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
