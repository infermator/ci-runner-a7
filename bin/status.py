#!/usr/bin/env python3
"""Post a commit status on the source repository. Idempotent: the newest status per context wins.

  SRC_REPO, SRC_TOKEN, SHA, TARGET_URL in the environment; usage: status.py STATE CONTEXT DESCRIPTION
Prints one fixed line; never prints the response or the repository.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    state, context, description = sys.argv[1], sys.argv[2], sys.argv[3]
    if state not in ('pending', 'success', 'failure', 'error'):
        print('status: invalid state')
        return 2
    repo, token, sha = os.environ.get('SRC_REPO', ''), os.environ.get('SRC_TOKEN', ''), os.environ.get('SHA', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not token or not re.fullmatch(r'[0-9a-f]{40}', sha):
        print(f'status {context}: not configured')
        return 1
    body = json.dumps({'state': state, 'context': context, 'description': description[:140],
                       'target_url': os.environ.get('TARGET_URL', '')}).encode()
    for attempt in range(4):
        req = urllib.request.Request(f'https://api.github.com/repos/{repo}/statuses/{sha}', data=body, method='POST', headers={
            'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ci-runner'})
        try:
            with urllib.request.urlopen(req, timeout=30):
                print(f'status {context}: {state}')
                return 0
        except urllib.error.HTTPError as e:
            code = e.code
            if code < 500 and code != 429:
                break
        except Exception:
            code = 'network'
        time.sleep(5 * (attempt + 1))
    print(f'status {context}: not posted ({code})')
    return 1


if __name__ == '__main__':
    sys.exit(main())
