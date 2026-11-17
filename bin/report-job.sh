#!/usr/bin/env bash
# End of one job: store its log and result in the log repository, then post the per-job status.
#   SHA, LABEL, RUN_DATE, TARGET_URL, SRC_REPO, SRC_TOKEN, LOG_REPO, LOG_TOKEN in the environment.
set -uo pipefail
bin=$(cd "$(dirname "$0")" && pwd)
result="$RUNNER_TEMP/result.json"
if [ ! -f "$result" ]; then
  printf '{"label":"%s","state":"error","steps_ok":0,"steps_run":0,"seconds":0,"reason":"job did not reach its steps"}\n' "$LABEL" >"$result"
fi
touch "$RUNNER_TEMP/job.log"
bash "$bin/push-log.sh" "$RUN_DATE" "$RUNNER_TEMP/job.log" "$LABEL.log" "$result" "$LABEL.json" || true
read -r state desc < <(python3 - "$result" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
state = r.get('state') if r.get('state') in ('success', 'failure', 'error') else 'error'
print(state, f"steps {r.get('steps_ok', 0)}/{r.get('steps_run', 0)} in {round(r.get('seconds', 0))}s")
PY
)
python3 "$bin/status.py" "${state:-error}" "ci/quality/$LABEL" "${desc:-no result}"
