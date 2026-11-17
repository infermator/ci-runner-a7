#!/usr/bin/env bash
# Fetch the source tree at one commit into DEST without persisting credentials.
#   SRC_REPO, SRC_TOKEN, SHA in the environment; usage: fetch-source.sh DEST LOGFILE
# Git output goes to LOGFILE only. The token is passed as a one-shot header and never written to git config.
set -uo pipefail
dest=${1:?dest}
log=${2:?log}
[[ ${SHA:-} =~ ^[0-9a-f]{40}$ ]] || { echo "invalid commit id"; exit 2; }
[[ ${SRC_REPO:-} =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "source not configured"; exit 2; }
[[ -n ${SRC_TOKEN:-} ]] || { echo "token not configured"; exit 2; }

auth=$(printf 'x-access-token:%s' "$SRC_TOKEN" | base64 -w0)
echo "::add-mask::$auth"

(
  set -e
  mkdir -p "$dest"
  cd "$dest"
  git init -q
  git remote add origin "https://github.com/${SRC_REPO}"
  git -c credential.helper= -c "http.https://github.com/.extraheader=AUTHORIZATION: basic ${auth}" -c protocol.version=2 \
    fetch --prune --no-recurse-submodules origin \
    '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*' "+${SHA}:refs/ci-target"
  git checkout -q --detach "$SHA"
  git update-ref -d refs/ci-target
  test "$(git rev-parse HEAD)" = "$SHA"
) >>"$log" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "source fetch failed"
  exit 1
fi
echo "source fetched"
