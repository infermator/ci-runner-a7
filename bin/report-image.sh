#!/usr/bin/env bash
# End of an image run: store its log and result in the log repository, then post the `ci/image` status on the source commit.
#   SHA, RUN_DATE, TARGET_URL, SRC_REPO, SRC_TOKEN, LOG_REPO, LOG_TOKEN in the environment.
set -uo pipefail
bin=$(cd "$(dirname "$0")" && pwd)
result="$RUNNER_TEMP/image-result.json"
if [ ! -f "$result" ]; then
  printf '{"stage":"none","state":"error","reason":"run did not reach its steps"}\n' >"$result"
fi
touch "$RUNNER_TEMP/image.log"
bash "$bin/push-log.sh" "$RUN_DATE" "$RUNNER_TEMP/image.log" image.log "$result" image.json || true
read -r state desc < <(python3 - "$bin" "$result" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from image import summarize
try:
    r = json.load(open(sys.argv[2]))
except ValueError:
    r = {}
print(*summarize(r))
PY
)
echo "result: ${state:-error}"
python3 "$bin/status.py" "${state:-error}" ci/image "${desc:-no result}"
