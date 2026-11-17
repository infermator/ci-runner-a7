#!/usr/bin/env python3
"""Find source commits that have no result yet and dispatch a test run for each.

Candidates: the head commit of each open pull request (most recently updated first, at most MAX_PRS) and the head of
the default branch. A commit is skipped when it already carries the aggregate status, unless that status is a
`pending` older than STALE_HOURS (a run that died without reporting).

  SRC_REPO, SRC_TOKEN       read the source repository and post the queued status
  DISPATCH_TOKEN, SELF_REPO dispatch the test workflow in this repository
Prints only counts.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

CONTEXT = 'ci/quality'
MAX_PRS = 3
STALE_HOURS = 6
WORKFLOW = 'quality.yml'
SUITES = 'quality sql'


class ApiError(Exception):
    pass


def api(method: str, path: str, token: str, body=None):
    req = urllib.request.Request(f'https://api.github.com{path}', method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                                          'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ci-runner'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise ApiError(str(e.code)) from None
    except Exception:
        raise ApiError('network') from None


def needs_run(repo: str, token: str, sha: str) -> bool:
    combined = api('GET', f'/repos/{repo}/commits/{sha}/status?per_page=100', token)
    for s in combined.get('statuses', []):
        if s.get('context') != CONTEXT:
            continue
        if s.get('state') != 'pending':
            return False
        updated = dt.datetime.fromisoformat(s['updated_at'].replace('Z', '+00:00'))
        return dt.datetime.now(dt.timezone.utc) - updated > dt.timedelta(hours=STALE_HOURS)
    return True


def main() -> int:
    repo, token = os.environ.get('SRC_REPO', ''), os.environ.get('SRC_TOKEN', '')
    self_repo, dispatch = os.environ.get('SELF_REPO', ''), os.environ.get('DISPATCH_TOKEN', '')
    if not (re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) and token and self_repo and dispatch):
        print('poll: not configured')
        return 1
    try:
        pulls = api('GET', f'/repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=30', token)
        default = api('GET', f'/repos/{repo}', token)['default_branch']
        main_sha = api('GET', f'/repos/{repo}/branches/{default}', token)['commit']['sha']
    except ApiError as e:
        print(f'poll: source not readable ({e})')
        return 1

    queue: list[str] = []
    checked = 0
    for pr in pulls:
        if len(queue) >= MAX_PRS:
            break
        sha = pr['head']['sha']
        if sha in queue:
            continue
        checked += 1
        try:
            if needs_run(repo, token, sha):
                queue.append(sha)
        except ApiError:
            continue
    try:
        if main_sha not in queue and needs_run(repo, token, main_sha):
            queue.append(main_sha)
    except ApiError:
        pass

    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{self_repo}/actions/workflows/{WORKFLOW}"
    dispatched = 0
    for sha in queue:
        try:
            api('POST', f'/repos/{self_repo}/actions/workflows/{WORKFLOW}/dispatches', dispatch,
                {'ref': os.environ.get('SELF_REF', 'main'), 'inputs': {'sha': sha, 'suites': SUITES}})
        except ApiError:
            continue
        dispatched += 1
        try:
            api('POST', f'/repos/{repo}/statuses/{sha}', token,
                {'state': 'pending', 'context': CONTEXT, 'description': 'queued', 'target_url': run_url})
        except ApiError:
            pass
    print(f'poll: {checked} checked, {len(queue)} without result, {dispatched} dispatched')
    return 0 if dispatched == len(queue) else 1


if __name__ == '__main__':
    sys.exit(main())
