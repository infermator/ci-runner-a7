#!/usr/bin/env bash
# End-to-end check of bin/image.py on a synthetic source repository: prepare, build (with an excluded path and a public build argument), boundary
# checks, archive identity, signature verified independently with ssh-keygen; and a second commit that leaks a forbidden name into the browser
# files, which must fail the build. Synthetic data only; prints the private log on failure.
set -euo pipefail
runner=$(cd "$(dirname "$0")/.." && pwd)
w=${RUNNER_TEMP:-/tmp}/e2e
rm -rf "$w"
mkdir -p "$w/src"
trap 'echo "--- log tail"; tail -n 80 "$w/log" 2>/dev/null || true' ERR

cd "$w/src"
git init -q -b main
git config user.email selftest@example.invalid
git config user.name selftest
mkdir -p spec skipme
echo private > skipme/data.txt
cat > Dockerfile <<'EOF'
FROM node:24-bookworm-slim
ARG PUBLIC_VALUE
ARG MODE=false
COPY . /ctx
RUN test ! -e /ctx/skipme && test -f /ctx/Dockerfile && rm -rf /ctx
WORKDIR /app
RUN mkdir -p .next/static && echo 'console.log(1)' > server.js && echo b1 > .next/BUILD_ID \
 && echo "var v='${PUBLIC_VALUE}', m='${MODE}';" > .next/static/a.js && chown -R 1001:1001 /app
USER 1001
CMD ["node", "server.js"]
EOF
cat > spec/image-spec.json <<'EOF'
{"schema": "image-spec.v1", "image_tag": "selftest-web:rc-{sha}", "platform": "linux/amd64", "exclude": ["skipme"],
 "build_args": {"PUBLIC_VALUE": "{public_key}", "MODE": "true"},
 "checks": {"uid": "1001", "files": ["server.js", ".next/BUILD_ID"], "node_check": ["server.js"], "forbidden_dir": ".next/static",
            "forbidden": ["SERVER_ONLY_SECRET", "PRIVATE_DB_URL"]},
 "manifest": {"platform": "linux/amd64", "mode": true, "public_origin": "https://example.invalid"},
 "signature": {"namespace": "selftest-artifact", "header": "selftest-artifact-v1"},
 "publish": {"release_tag": "selftest", "asset_prefix": "web-", "keep": 3}}
EOF
git add -A
git commit -qm one
sha=$(git rev-parse HEAD)

img() { python3 "$runner/bin/image.py" "$@" --log "$w/log" --result "$w/result.json"; }
export PUBLIC_KEY=public-value-123
img prepare --sha "$sha" --src "$w/src" --spec-path spec/image-spec.json --work "$w/work"
img build --sha "$sha" --src "$w/src" --work "$w/work" --run-date 2026-01-01
ssh-keygen -q -t ed25519 -N '' -C selftest -f "$w/key"
SIGNING_KEY="$(cat "$w/key")" img sign --sha "$sha" --work "$w/work"

python3 - "$w/work" "$sha" <<'PY'
import gzip, hashlib, json, sys, tarfile
from pathlib import Path
work, sha = Path(sys.argv[1]), sys.argv[2]
m = json.loads((work / 'manifest.json').read_text())
digest = hashlib.sha256((work / 'image.tar.gz').read_bytes()).hexdigest()
assert m['release_sha'] == sha and m['archive_sha256'] == digest, 'manifest'
assert m['image_tag'] == f'selftest-web:rc-{sha}' and m['mode'] is True and m['platform'] == 'linux/amd64', 'fixed fields'
with tarfile.open(work / 'image.tar.gz', 'r:gz') as tar:
    entry = json.load(tar.extractfile('manifest.json'))[0]
    config = tar.extractfile(entry['Config']).read()
assert entry['RepoTags'] == [m['image_tag']], 'tag'
assert 'sha256:' + hashlib.sha256(config).hexdigest() == m['image_id'], 'image id'
labels = json.loads(config)['config']['Labels']
assert labels['org.opencontainers.image.revision'] == sha, 'revision label'
print('manifest and archive ok')
PY
id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image_id"])' "$w/work/manifest.json")
digest=$(sha256sum "$w/work/image.tar.gz" | cut -d' ' -f1)
printf 'builder namespaces="selftest-artifact" %s\n' "$(cat "$w/key.pub")" > "$w/allowed"
printf 'selftest-artifact-v1\nrelease_sha: %s\nimage_id: %s\narchive_sha256: %s\n' "$sha" "$id" "$digest" |
  ssh-keygen -Y verify -f "$w/allowed" -I builder -n selftest-artifact -s "$w/work/image.sig"
docker run --rm --network none --entrypoint sh "selftest-web:rc-$sha" -c 'grep -q public-value-123 .next/static/a.js'
echo "public build argument baked in"

# A forbidden server-only name in the browser files must fail the build.
sed -i 's/m=/SERVER_ONLY_SECRET=/' Dockerfile
git commit -qam leak
leak=$(git rev-parse HEAD)
img prepare --sha "$leak" --src "$w/src" --spec-path spec/image-spec.json --work "$w/work2"
if img build --sha "$leak" --src "$w/src" --work "$w/work2" --run-date 2026-01-01; then
  echo "leak was not detected"; exit 1
fi
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); assert (r["stage"], r["state"]) == ("build", "failure"), r' "$w/result.json"
echo "leak detected"
echo "selftest: image pipeline ok"
