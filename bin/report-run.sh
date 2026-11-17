#!/usr/bin/env bash
# End of a run: the aggregate status is success only when every requested group succeeded.
#   SHA, RUN_DATE, RUN_ID, TARGET_URL, WANT_APP, WANT_DB, APP_RESULT, DB_RESULT, SRC_*, LOG_* in the environment.
set -uo pipefail
bin=$(cd "$(dirname "$0")" && pwd)
state=success
desc=passed
check() {  # want result
  [ "$1" = true ] || return 0
  case "$2" in
    success) ;;
    failure) state=failure desc=failed ;;
    cancelled) [ "$state" = failure ] || { state=error desc=cancelled; } ;;
    *) [ "$state" = failure ] || { state=error desc="did not run"; } ;;
  esac
}
check "$WANT_APP" "$APP_RESULT"
check "$WANT_DB" "$DB_RESULT"
summary="$RUNNER_TEMP/summary.json"
python3 - "$summary" "$state" <<'PY'
import json, os, sys, datetime
want = lambda k: os.environ.get(k) == 'true'
json.dump({
    'sha': os.environ['SHA'],
    'state': sys.argv[2],
    'run_id': os.environ.get('RUN_ID'),
    'run_url': os.environ.get('TARGET_URL'),
    'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
    'groups': {
        'quality': os.environ.get('APP_RESULT') if want('WANT_APP') else 'not requested',
        'sql': os.environ.get('DB_RESULT') if want('WANT_DB') else 'not requested',
    },
}, open(sys.argv[1], 'w'), indent=2)
PY
bash "$bin/push-log.sh" "$RUN_DATE" "$summary" summary.json || true
echo "result: $state"
python3 "$bin/status.py" "$state" ci/quality "$desc"
