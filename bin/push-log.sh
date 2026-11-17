#!/usr/bin/env bash
# Store files in the private log repository under runs/DATE/SHA/NAME.
#   LOG_REPO, LOG_TOKEN, SHA in the environment; usage: push-log.sh DATE FILE NAME [FILE NAME ...]
# Each run writes its own paths, so concurrent pushes never conflict on content: a rejected push re-reads the
# branch head, re-applies the same files on top and retries. Git output is discarded; the output is one fixed line.
set -uo pipefail
date=${1:?date}
shift
[[ $date =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "log: invalid date"; exit 2; }
[[ ${SHA:-} =~ ^[0-9a-f]{40}$ ]] || { echo "log: invalid commit id"; exit 2; }
[[ ${LOG_REPO:-} =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "log: not configured"; exit 1; }
[[ -n ${LOG_TOKEN:-} ]] || { echo "log: not configured"; exit 1; }

srcs=() names=()
while [ "$#" -ge 2 ]; do
  [[ $2 =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "log: invalid name"; exit 2; }
  if [ -f "$1" ]; then srcs+=("$1"); names+=("$2"); fi
  shift 2
done
[ "${#srcs[@]}" -gt 0 ] || { echo "log: nothing to store"; exit 0; }

auth=$(printf 'x-access-token:%s' "$LOG_TOKEN" | base64 -w0)
echo "::add-mask::$auth"
work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/logs.XXXXXX")
repo="$work/repo"
dir="runs/${date}/${SHA}"
LIMIT=$((45 * 1024 * 1024))

g() { git -c credential.helper= -c "http.https://github.com/.extraheader=AUTHORIZATION: basic ${auth}" "$@"; }

stage() {
  # Copy every file into the tree and commit on top of the current checkout.
  mkdir -p "$repo/$dir" || return 1
  local i src dst size
  for i in "${!srcs[@]}"; do
    src=${srcs[$i]} dst="$repo/$dir/${names[$i]}"
    size=$(wc -c <"$src" | tr -d " ")
    if [ "$size" -gt "$LIMIT" ]; then
      # GitHub refuses files over 100 MB: keep the head and the tail, which hold the setup and the failure.
      { head -c $((5 * 1024 * 1024)) "$src"
        printf '\n\n##### %s bytes omitted #####\n\n' "$((size - 40 * 1024 * 1024))"
        tail -c $((35 * 1024 * 1024)) "$src"; } >"$dst" || return 1
    else
      cp "$src" "$dst" || return 1
    fi
  done
  git -C "$repo" add -A runs && { git -C "$repo" diff --cached --quiet || git -C "$repo" commit -q -m "logs ${SHA:0:12} ${date}"; }
}

{
  g clone -q --depth 1 "https://github.com/${LOG_REPO}" "$repo" &&
    git -C "$repo" config user.name ci-bot &&
    git -C "$repo" config user.email ci-bot@users.noreply.github.com &&
    stage
} >/dev/null 2>&1 || { echo "log: not stored (prepare)"; exit 1; }

for attempt in 1 2 3 4 5 6; do
  if g -C "$repo" push -q origin HEAD:main >/dev/null 2>&1; then
    echo "log: stored"
    exit 0
  fi
  sleep $((attempt * 3 + RANDOM % 5))
  { g -C "$repo" fetch -q --depth 1 origin main && git -C "$repo" reset -q --hard FETCH_HEAD && stage; } >/dev/null 2>&1 || true
done
echo "log: not stored (push)"
exit 1
