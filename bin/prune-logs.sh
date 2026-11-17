#!/usr/bin/env bash
# Keep the private log repository small: delete runs/<date> folders older than KEEP_DAYS and replace the branch
# history with one commit of what remains. The force push is guarded by a lease on the head that was read, so a log
# pushed meanwhile is never lost: the lease fails and the pass starts again.
#   LOG_REPO, LOG_TOKEN in the environment. Output: one fixed line.
set -uo pipefail
KEEP_DAYS=${KEEP_DAYS:-30}
[[ ${LOG_REPO:-} =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "prune: not configured"; exit 1; }
[[ -n ${LOG_TOKEN:-} ]] || { echo "prune: not configured"; exit 1; }
auth=$(printf 'x-access-token:%s' "$LOG_TOKEN" | base64 -w0)
echo "::add-mask::$auth"
g() { git -c credential.helper= -c "http.https://github.com/.extraheader=AUTHORIZATION: basic ${auth}" "$@"; }
cutoff=$(python3 -c "import datetime as d,sys; print((d.datetime.now(d.timezone.utc)-d.timedelta(days=int(sys.argv[1]))).date())" "$KEEP_DAYS")

for attempt in 1 2 3 4 5; do
  work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/prune.XXXXXX")
  repo="$work/repo"
  if ! g clone -q --depth 1 "https://github.com/${LOG_REPO}" "$repo" >/dev/null 2>&1; then
    echo "prune: clone failed"
    exit 1
  fi
  head=$(git -C "$repo" rev-parse HEAD)
  removed=0
  if [ -d "$repo/runs" ]; then
    for d in "$repo"/runs/*/; do
      day=$(basename "$d")
      [[ $day =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue
      if [[ $day < $cutoff ]]; then
        rm -rf "$d"
        removed=$((removed + 1))
      fi
    done
  fi
  git -C "$repo" add -A >/dev/null 2>&1
  tree=$(git -C "$repo" write-tree)
  commit=$(GIT_AUTHOR_NAME=ci-bot GIT_AUTHOR_EMAIL=ci-bot@users.noreply.github.com GIT_COMMITTER_NAME=ci-bot \
    GIT_COMMITTER_EMAIL=ci-bot@users.noreply.github.com git -C "$repo" commit-tree "$tree" -m "logs since ${cutoff}")
  if g -C "$repo" push -q --force-with-lease="main:${head}" origin "${commit}:refs/heads/main" >/dev/null 2>&1; then
    echo "prune: ${removed} day folders removed, history squashed"
    exit 0
  fi
  rm -rf "$work"
  sleep $((attempt * 10))
done
echo "prune: push refused"
exit 1
