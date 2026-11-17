#!/usr/bin/env python3
"""Build, sign and privately store a container image of one source commit (the `image` workflow).

One subcommand per workflow step, so that each step holds only its own secret:

  prepare  read and validate the image spec at the commit (data only)                 no secret
  check    is the image of this commit already stored?                                 ART_REPO, ART_TOKEN
  build    `git archive` of the commit, build, check in a container without network,
           export a checksummed `docker save` archive and its manifest                 PUBLIC_KEY (optional)
  sign     sign commit, image id and archive digest with the builder key               SIGNING_KEY
  publish  upload archive, manifest and signature as release assets; prune old builds  ART_REPO, ART_TOKEN
  poll     dispatch a build for the source default-branch head without an image status SRC_REPO, SRC_TOKEN, DISPATCH_TOKEN, SELF_REPO

No source code runs on this host: the source's Dockerfile runs inside BuildKit's sandbox and the checks run in a container without network.
Privacy: everything a step produces goes to --log; standard output carries one fixed line per step. Stage results accumulate in --result.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCHEMA = 'image-spec.v1'
SHA_RE = re.compile(r'[0-9a-f]{40}')
REPO_RE = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
PATH_RE = re.compile(r'[A-Za-z0-9_.@+-]+(/[A-Za-z0-9_.@+-]+)*')
ARG_RE = re.compile(r'[A-Z][A-Z0-9_]{0,63}')
WORD_RE = re.compile(r'[A-Za-z0-9_]{3,64}')
TAG_RE = re.compile(r'[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}')
LABEL_RE = re.compile(r'[a-z0-9][a-z0-9.-]{0,79}')
KEY_RE = re.compile(r'[a-z][a-z0-9_]{0,63}')
ASSET_RE = re.compile(r'[A-Za-z0-9._-]{1,64}')
PREFIX_RE = re.compile(r'[A-Za-z0-9._-]{0,32}')
DIGEST_RE = re.compile(r'[0-9a-f]{64}')
IMAGE_ID_RE = re.compile(r'sha256:[0-9a-f]{64}')
PLACEHOLDER_RE = re.compile(r'\{[^{}]*\}')
PLATFORMS = ('linux/amd64', 'linux/arm64')
RESERVED = ('release_sha', 'image_id', 'image_tag', 'archive_sha256', 'build_provenance')
TOP_KEYS = {'schema', 'image_tag', 'platform', 'exclude', 'build_args', 'checks', 'manifest', 'signature', 'publish'}
CHECK_KEYS = {'uid', 'files', 'node_check', 'forbidden_dir', 'forbidden'}
REVISION_LABEL = 'org.opencontainers.image.revision'
SUFFIXES = (('.tar.gz', 'image.tar.gz', 'application/gzip'), ('.json', 'manifest.json', 'application/json'), ('.sig', 'image.sig', 'text/plain'))
MAX_ASSET_BYTES = 2 * 1024 ** 3 - 64 * 1024 ** 2  # GitHub release assets must be under 2 GiB
STATUS_CONTEXT = 'ci/image'
WORKFLOW = 'image.yml'
STALE_HOURS = 3
API = 'https://api.github.com'
UPLOADS = 'https://uploads.github.com'
USER_AGENT = 'ci-runner'
CHECK_SCRIPT = r'''
test "$(id -u)" = "$WANT_UID"
for f in $WANT_FILES; do test -f "$f"; done
for f in $WANT_NODE_CHECK; do node --check "$f"; done
test -d "$FORBIDDEN_DIR"
test -n "$(ls -A "$FORBIDDEN_DIR")"
rc=0
grep -r -E -l -- "$FORBIDDEN" "$FORBIDDEN_DIR" || rc=$?
test "$rc" = 1
'''


class Failure(Exception):
    """A reason for the private log; the public line names only the stage."""


# --------------------------------------------------------------------------------------------------------------------------------------------------
# Spec (pure)
# --------------------------------------------------------------------------------------------------------------------------------------------------

def _path(value, what):
    if not isinstance(value, str) or not PATH_RE.fullmatch(value) or any(part in ('.', '..') for part in value.split('/')):
        raise Failure(f'spec: {what} is not a safe relative path')
    return value


def _paths(value, what, low, high):
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise Failure(f'spec: {what} must be a list of {low} to {high} paths')
    return [_path(item, what) for item in value]


def _text(value, what, limit=4096):
    if not isinstance(value, str) or len(value) > limit or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise Failure(f'spec: {what} must be a single-line string')
    return value


def load_spec(raw: bytes, sha: str) -> dict:
    """Validate the source's image spec and return it normalized, with the image tag resolved for `sha`. Fails closed on anything unknown."""
    if not SHA_RE.fullmatch(sha or ''):
        raise Failure('invalid commit id')
    try:
        spec = json.loads(raw)
    except ValueError:
        raise Failure('spec: not JSON') from None
    if not isinstance(spec, dict) or spec.get('schema') != SCHEMA:
        raise Failure('spec: unknown schema')
    unknown = set(spec) - TOP_KEYS
    if unknown:
        raise Failure(f'spec: unknown keys {sorted(unknown)}')

    template = spec.get('image_tag')
    if not isinstance(template, str) or template.count('{sha}') != 1:
        raise Failure('spec: image_tag must contain {sha} exactly once')
    tag = template.replace('{sha}', sha)
    if not TAG_RE.fullmatch(tag):
        raise Failure('spec: image_tag is not a valid local image tag')

    platform = spec.get('platform')
    if platform not in PLATFORMS:
        raise Failure('spec: unsupported platform')

    exclude = _paths(spec.get('exclude', []), 'exclude', 0, 50)

    args = spec.get('build_args', {})
    if not isinstance(args, dict) or len(args) > 50:
        raise Failure('spec: build_args must be a mapping of at most 50 entries')
    build_args = {}
    for name, value in args.items():
        if not ARG_RE.fullmatch(name):
            raise Failure('spec: build arg name is not an upper-case identifier')
        _text(value, 'build arg value')
        if not set(PLACEHOLDER_RE.findall(value)) <= {'{sha}', '{public_key}'}:
            raise Failure('spec: build arg value has an unknown placeholder')
        build_args[name] = value

    checks = spec.get('checks')
    if not isinstance(checks, dict) or set(checks) != CHECK_KEYS:
        raise Failure(f'spec: checks must have exactly {sorted(CHECK_KEYS)}')
    if not isinstance(checks['uid'], str) or not checks['uid'].isdigit() or len(checks['uid']) > 10:
        raise Failure('spec: checks.uid must be a numeric string')
    forbidden = checks['forbidden']
    if not isinstance(forbidden, list) or not 1 <= len(forbidden) <= 50 or not all(isinstance(w, str) and WORD_RE.fullmatch(w) for w in forbidden):
        raise Failure('spec: checks.forbidden must list 1 to 50 identifiers')
    checks = {'uid': checks['uid'], 'files': _paths(checks['files'], 'checks.files', 1, 20),
              'node_check': _paths(checks['node_check'], 'checks.node_check', 0, 20),
              'forbidden_dir': _path(checks['forbidden_dir'], 'checks.forbidden_dir'), 'forbidden': list(forbidden)}

    fixed = spec.get('manifest', {})
    if not isinstance(fixed, dict) or len(fixed) > 20:
        raise Failure('spec: manifest must be a mapping of at most 20 entries')
    for key, value in fixed.items():
        if not KEY_RE.fullmatch(key) or key in RESERVED:
            raise Failure('spec: manifest key is reserved or invalid')
        if isinstance(value, str):
            _text(value, 'manifest value', 256)
        elif not isinstance(value, (bool, int)):
            raise Failure('spec: manifest values must be strings, booleans or integers')

    signature = spec.get('signature')
    if (not isinstance(signature, dict) or set(signature) != {'namespace', 'header'}
            or not all(isinstance(v, str) and LABEL_RE.fullmatch(v) for v in signature.values())):
        raise Failure('spec: signature must have a namespace and a header (lower-case labels)')

    publish = spec.get('publish')
    if (not isinstance(publish, dict) or set(publish) != {'release_tag', 'asset_prefix', 'keep'}
            or not isinstance(publish['release_tag'], str) or not ASSET_RE.fullmatch(publish['release_tag'])
            or not isinstance(publish['asset_prefix'], str) or not PREFIX_RE.fullmatch(publish['asset_prefix'])
            or type(publish['keep']) is not int or not 1 <= publish['keep'] <= 30):
        raise Failure('spec: publish must have release_tag, asset_prefix and keep (1-30)')

    return {'image_tag': tag, 'platform': platform, 'exclude': exclude, 'build_args': build_args, 'checks': checks,
            'manifest': dict(fixed), 'signature': dict(signature), 'publish': dict(publish)}


def resolve_args(build_args: dict, sha: str, public_key: str) -> dict:
    out = {}
    for name, value in build_args.items():
        if '{public_key}' in value:
            if not public_key or any(ch.isspace() for ch in public_key):
                raise Failure('public key not configured')
            value = value.replace('{public_key}', public_key)
        out[name] = value.replace('{sha}', sha)
    return out


def artifact_manifest(spec: dict, sha: str, image_id: str, digest: str, provenance: str) -> dict:
    return {'release_sha': sha, 'image_id': image_id, 'image_tag': spec['image_tag'], 'archive_sha256': digest, **spec['manifest'],
            'build_provenance': provenance}


def signed_message(header: str, sha: str, image_id: str, digest: str) -> bytes:
    if not (LABEL_RE.fullmatch(header) and SHA_RE.fullmatch(sha) and IMAGE_ID_RE.fullmatch(image_id) and DIGEST_RE.fullmatch(digest)):
        raise Failure('sign: invalid message fields')
    return f'{header}\nrelease_sha: {sha}\nimage_id: {image_id}\narchive_sha256: {digest}\n'.encode('ascii')


def asset_names(prefix: str, sha: str) -> list[str]:
    return [f'{prefix}{sha}{suffix}' for suffix, _, _ in SUFFIXES]


def stored(assets: list, prefix: str, sha: str) -> bool:
    names = {a.get('name') for a in assets if isinstance(a, dict) and a.get('state') == 'uploaded'}
    return all(name in names for name in asset_names(prefix, sha))


def prune_plan(assets: list, prefix: str, keep: int) -> list:
    """Asset ids to delete so that only the newest `keep` commits (by newest upload) stay stored."""
    by_sha: dict[str, list] = {}
    for asset in assets:
        name = asset.get('name', '') if isinstance(asset, dict) else ''
        for suffix, _, _ in SUFFIXES:
            core = name[len(prefix):-len(suffix)] if name.startswith(prefix) and name.endswith(suffix) else ''
            if SHA_RE.fullmatch(core):
                by_sha.setdefault(core, []).append(asset)
                break
    order = sorted(by_sha, key=lambda sha: max(a.get('created_at', '') for a in by_sha[sha]), reverse=True)
    return [a['id'] for sha in order[keep:] for a in by_sha[sha]]


def archive_identity(archive: Path) -> tuple[list, str]:
    """(RepoTags, image id) of a single-image `docker save` archive, read without extracting it."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if any(not (m.isfile() or m.isdir()) for m in members):
            # Consumers that validate the archive without extracting it accept regular files and directories only.
            raise Failure("archive: contains links or special members")
        names = {m.name: m for m in members if m.isfile()}
        if 'manifest.json' not in names:
            raise Failure('archive: no manifest.json')
        entries = json.load(tar.extractfile(names['manifest.json']))
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise Failure('archive: not exactly one image')
        config = entries[0].get('Config')
        if config not in names:
            raise Failure('archive: image config missing')
        digest = hashlib.sha256(tar.extractfile(names[config]).read()).hexdigest()
        return entries[0].get('RepoTags'), f'sha256:{digest}'


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 ** 2), b''):
            digest.update(block)
    return digest.hexdigest()


def summarize(result: dict) -> tuple[str, str]:
    """Commit status (state, description) for the run from the accumulated stage results."""
    stage, state = result.get('stage', 'none'), result.get('state')
    if state == 'success' and stage == 'publish':
        return 'success', f"stored {result.get('image_id', '')[:19]}"
    if state == 'success' and stage == 'check' and result.get('stored'):
        return 'success', 'already stored'
    if state == 'failure':
        return 'failure', f'failed at {stage}'
    return 'error', f'incomplete after {stage}'


# --------------------------------------------------------------------------------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------------------------------------------------------------------------------

class Log:
    def __init__(self, path):
        self.handle = open(path, 'a', encoding='utf-8', errors='replace')

    def note(self, message):
        self.handle.write(f'\n##### {message}\n')
        self.handle.flush()

    def run(self, args, *, env=None, input=None, stdout=None, timeout=None, cwd=None):
        self.note('$ ' + ' '.join(str(a) for a in args))
        done = subprocess.run([str(a) for a in args], env=env, input=input, stdout=stdout or self.handle, stderr=self.handle,
                              stdin=None if input is not None else subprocess.DEVNULL, timeout=timeout, cwd=cwd)
        if done.returncode:
            raise Failure(f'{args[0]} {args[1] if len(args) > 1 else ""} exited {done.returncode}')
        return done

    def out(self, args, **kw):
        return self.run(args, stdout=subprocess.PIPE, **kw).stdout.decode('utf-8', 'replace').strip()


def record(path: Path, **fields):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data.update(fields)
    data['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    path.write_text(json.dumps(data, indent=1) + '\n')


def gh(method, url, token, payload=None, *, data=None, content_type=None, length=None, timeout=60):
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
               'User-Agent': USER_AGENT}
    if payload is not None:
        data, content_type = json.dumps(payload).encode(), 'application/json'
    if content_type:
        headers['Content-Type'] = content_type
    if length is not None:
        headers['Content-Length'] = str(length)
    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else None


def art_config():
    repo, token = os.environ.get('ART_REPO', ''), os.environ.get('ART_TOKEN', '')
    if not REPO_RE.fullmatch(repo) or not token:
        raise Failure('artifact store not configured')
    return repo, token


def release(repo, token, tag, *, create):
    try:
        return gh('GET', f'{API}/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}', token)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    if not create:
        return None
    return gh('POST', f'{API}/repos/{repo}/releases', token, {
        'tag_name': tag, 'name': tag, 'prerelease': True,
        'body': 'Build artifacts written by an automated builder. Only the newest builds are kept; never attach anything by hand.'})


def list_assets(repo, token, release_id):
    out, page = [], 1
    while True:
        chunk = gh('GET', f'{API}/repos/{repo}/releases/{int(release_id)}/assets?per_page=100&page={page}', token)
        out.extend(chunk or [])
        if not chunk or len(chunk) < 100:
            return out
        page += 1


# --------------------------------------------------------------------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------------------------------------------------------------------

def cmd_prepare(a, log):
    _path(a.spec_path, 'spec path')
    src = Path(a.src)
    raw = None
    for rev in (a.sha, 'refs/remotes/origin/main'):
        done = subprocess.run(['git', '-C', str(src), 'show', f'{rev}:{a.spec_path}'], stdout=subprocess.PIPE, stderr=log.handle)
        if done.returncode == 0:
            log.note(f'image spec read from {rev}')
            raw = done.stdout
            break
    if raw is None:
        raise Failure('spec: not found at the commit or on the default branch')
    spec = load_spec(raw, a.sha)
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    (work / 'spec.json').write_text(json.dumps(spec, indent=1) + '\n')
    log.note(f"spec ok: image {spec['image_tag']}, {len(spec['build_args'])} build args, {len(spec['checks']['forbidden'])} forbidden names")
    return {}


def cmd_check(a, log):
    spec = json.loads((Path(a.work) / 'spec.json').read_text())
    repo, token = art_config()
    rel = release(repo, token, spec['publish']['release_tag'], create=False)
    found = bool(rel) and stored(list_assets(repo, token, rel['id']), spec['publish']['asset_prefix'], a.sha)
    log.note(f'stored already: {found}')
    return {'stored': found}


def ensure_classic_store(log):
    """`docker save` must write the classic layout (layer tars plus a config whose digest is the image id), which the containerd image store does
    not guarantee. Switch the throw-away runner's daemon back to the classic store when needed."""
    if 'containerd' not in log.out(['docker', 'info', '--format', '{{json .DriverStatus}}']):
        return
    log.note('containerd image store active: switching the daemon to the classic store')
    path = '/etc/docker/daemon.json'
    current = subprocess.run(['sudo', '-n', 'cat', path], capture_output=True)
    try:
        conf = json.loads(current.stdout) if current.returncode == 0 and current.stdout.strip() else {}
    except ValueError:
        raise Failure('docker daemon.json unreadable') from None
    conf.setdefault('features', {})['containerd-snapshotter'] = False
    log.run(['sudo', '-n', 'tee', path], input=json.dumps(conf, indent=1).encode())
    log.run(['sudo', '-n', 'systemctl', 'restart', 'docker'], timeout=300)
    if 'containerd' in log.out(['docker', 'info', '--format', '{{json .DriverStatus}}']):
        raise Failure('docker still uses the containerd image store')


def cmd_build(a, log):
    work = Path(a.work)
    spec = json.loads((work / 'spec.json').read_text())
    sha, tag, platform = a.sha, spec['image_tag'], spec['platform']
    args = resolve_args(spec['build_args'], sha, os.environ.get('PUBLIC_KEY', ''))
    ensure_classic_store(log)

    ctx = work / 'context'
    shutil.rmtree(ctx, ignore_errors=True)
    ctx.mkdir(parents=True)
    pathspec = ['.'] + [f':(exclude){p}' for p in spec['exclude']]
    log.note(f'context: git archive {sha}, excluding {len(spec["exclude"])} paths')
    archiver = subprocess.Popen(['git', '-C', a.src, 'archive', '--format=tar', sha, '--', *pathspec], stdout=subprocess.PIPE,
                                stderr=log.handle)
    untar = subprocess.run(['tar', '-x', '-f', '-', '-C', str(ctx), '--no-same-owner'], stdin=archiver.stdout, stderr=log.handle)
    archiver.stdout.close()
    if archiver.wait() != 0 or untar.returncode != 0:
        raise Failure('context: git archive failed')
    for p in spec['exclude']:
        if (ctx / p).exists():
            raise Failure('context: an excluded path is present')

    # Build args are passed by name only; docker reads their values from its environment, so none appears in an argument list or the log.
    env = dict(os.environ)
    env.pop('PUBLIC_KEY', None)
    clash = [name for name in args if name in env]
    if clash:
        raise Failure(f'spec: build arg names clash with the runner environment: {clash}')
    env.update(args)
    command = ['docker', 'buildx', 'build', '--platform', platform, '--provenance=false', '--load',
               '--label', f'{REVISION_LABEL}={sha}', '--tag', tag]
    for name in args:
        command += ['--build-arg', name]
    log.run(['nice', '-n', '5', *command, ctx], env=env, timeout=50 * 60)
    shutil.rmtree(ctx, ignore_errors=True)

    if log.out(['docker', 'image', 'inspect', tag, '--format', '{{index .Config.Labels "' + REVISION_LABEL + '"}}']) != sha:
        raise Failure('image: revision label differs')
    if log.out(['docker', 'image', 'inspect', tag, '--format', '{{.Os}}/{{.Architecture}}']) != platform:
        raise Failure('image: platform differs')
    image_id = log.out(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'])
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise Failure('image: unexpected id format')

    checks = spec['checks']
    check_env = {'WANT_UID': checks['uid'], 'WANT_FILES': ' '.join(checks['files']), 'WANT_NODE_CHECK': ' '.join(checks['node_check']),
                 'FORBIDDEN_DIR': checks['forbidden_dir'], 'FORBIDDEN': '|'.join(checks['forbidden'])}
    flags = [x for k, v in check_env.items() for x in ('-e', f'{k}={v}')]
    log.note('boundary check: runtime user, required files, syntax, server-only names in browser files')
    log.run(['docker', 'run', '--rm', '--platform', platform, '--network', 'none', '--entrypoint', 'sh', *flags, tag, '-ec', CHECK_SCRIPT],
            timeout=15 * 60)

    archive = work / 'image.tar.gz'
    with open(archive, 'wb') as target:
        saver = subprocess.Popen(['docker', 'save', tag], stdout=subprocess.PIPE, stderr=log.handle)
        gz = subprocess.run(['gzip', '-1'], stdin=saver.stdout, stdout=target, stderr=log.handle)
        saver.stdout.close()
        if saver.wait() != 0 or gz.returncode != 0:
            raise Failure('docker save failed')
    size = archive.stat().st_size
    if not 0 < size <= MAX_ASSET_BYTES:
        raise Failure('archive: empty or too large for a release asset')
    digest = file_sha256(archive)
    tags, archived_id = archive_identity(archive)
    if tags != [tag] or archived_id != image_id:
        raise Failure('archive: image tag or id differs from the built image')

    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    provenance = f'{run_url} (git archive {sha}; full log: runs/{a.run_date}/{sha}/image.log in the private log repository)'
    manifest = artifact_manifest(spec, sha, image_id, digest, provenance)
    (work / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    log.note(f'archive {size} bytes, sha256 {digest}, image {image_id}')
    return {'image_id': image_id, 'archive_sha256': digest, 'archive_bytes': size}


def cmd_sign(a, log):
    work = Path(a.work)
    spec = json.loads((work / 'spec.json').read_text())
    manifest = json.loads((work / 'manifest.json').read_text())
    key = os.environ.get('SIGNING_KEY', '').replace('\r', '').strip()
    if not key.startswith('-----BEGIN OPENSSH PRIVATE KEY-----'):
        raise Failure('signing key not configured')
    digest = file_sha256(work / 'image.tar.gz')
    if manifest.get('release_sha') != a.sha or manifest.get('archive_sha256') != digest:
        raise Failure('sign: manifest does not describe this commit and archive')
    tags, image_id = archive_identity(work / 'image.tar.gz')
    if manifest.get('image_id') != image_id or tags != [spec['image_tag']]:
        raise Failure('sign: manifest image differs from the archive')
    namespace = spec['signature']['namespace']
    message = signed_message(spec['signature']['header'], a.sha, image_id, digest)
    with tempfile.TemporaryDirectory(dir=os.environ.get('RUNNER_TEMP')) as directory:
        d = Path(directory)
        key_file = d / 'key'
        with open(os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as handle:
            handle.write(key + '\n')
        (d / 'message').write_bytes(message)
        log.run(['ssh-keygen', '-Y', 'sign', '-f', key_file, '-n', namespace, d / 'message'])
        public = log.out(['ssh-keygen', '-y', '-f', key_file])
        key_file.unlink()
        (d / 'key.pub').write_text(public + '\n')
        fingerprint = log.out(['ssh-keygen', '-lf', d / 'key.pub']).split()[1]
        (d / 'allowed').write_text(f'builder namespaces="{namespace}" {public}\n')
        log.run(['ssh-keygen', '-Y', 'verify', '-f', d / 'allowed', '-I', 'builder', '-n', namespace, '-s', d / 'message.sig'], input=message)
        signature = (d / 'message.sig').read_text(encoding='ascii')
    if not signature.startswith('-----BEGIN SSH SIGNATURE-----'):
        raise Failure('sign: unexpected signature format')
    (work / 'image.sig').write_text(signature if signature.endswith('\n') else signature + '\n', encoding='ascii')
    log.note(f'signed with key {fingerprint}')
    return {'key': fingerprint}


def cmd_publish(a, log):
    work = Path(a.work)
    spec = json.loads((work / 'spec.json').read_text())
    manifest = json.loads((work / 'manifest.json').read_text())
    if manifest.get('archive_sha256') != file_sha256(work / 'image.tar.gz') or not (work / 'image.sig').is_file():
        raise Failure('publish: archive, manifest and signature do not belong together')
    repo, token = art_config()
    prefix, keep = spec['publish']['asset_prefix'], spec['publish']['keep']
    rel = release(repo, token, spec['publish']['release_tag'], create=True)
    for (suffix, local, content_type), name in zip(SUFFIXES, asset_names(prefix, a.sha)):
        path = work / local
        for attempt in range(3):
            for asset in list_assets(repo, token, rel['id']):
                if asset.get('name') == name:
                    gh('DELETE', f"{API}/repos/{repo}/releases/assets/{int(asset['id'])}", token)
            try:
                with open(path, 'rb') as handle:
                    gh('POST', f"{UPLOADS}/repos/{repo}/releases/{int(rel['id'])}/assets?name={urllib.parse.quote(name)}", token,
                       data=handle, content_type=content_type, length=path.stat().st_size, timeout=1800)
                log.note(f'uploaded {name}')
                break
            except (urllib.error.URLError, OSError) as error:
                code = getattr(error, 'code', 'network')
                log.note(f'upload of {name} failed ({code}), attempt {attempt + 1}')
                if attempt == 2 or (isinstance(code, int) and code < 500 and code != 429):
                    raise Failure(f'publish: upload failed ({code})') from None
                time.sleep(10 * (attempt + 1))
    assets = list_assets(repo, token, rel['id'])
    if not stored(assets, prefix, a.sha):
        raise Failure('publish: assets not all present after upload')
    removed = 0
    for asset_id in prune_plan(assets, prefix, keep):
        gh('DELETE', f'{API}/repos/{repo}/releases/assets/{int(asset_id)}', token)
        removed += 1
    log.note(f'published; {removed} old assets removed')
    return {'stored': True, 'pruned_assets': removed}


def cmd_poll(a, log):
    repo, token = os.environ.get('SRC_REPO', ''), os.environ.get('SRC_TOKEN', '')
    self_repo, dispatch = os.environ.get('SELF_REPO', ''), os.environ.get('DISPATCH_TOKEN', '')
    if not (REPO_RE.fullmatch(repo) and token and REPO_RE.fullmatch(self_repo) and dispatch):
        raise Failure('poll not configured')
    default = gh('GET', f'{API}/repos/{repo}', token)['default_branch']
    head = gh('GET', f'{API}/repos/{repo}/branches/{urllib.parse.quote(default)}', token)['commit']['sha']
    combined = gh('GET', f'{API}/repos/{repo}/commits/{head}/status?per_page=100', token)
    for status in combined.get('statuses', []):
        if status.get('context') != STATUS_CONTEXT:
            continue
        updated = dt.datetime.fromisoformat(status['updated_at'].replace('Z', '+00:00'))
        if status.get('state') != 'pending' or dt.datetime.now(dt.timezone.utc) - updated < dt.timedelta(hours=STALE_HOURS):
            return {'dispatched': 0}
    gh('POST', f'{API}/repos/{self_repo}/actions/workflows/{WORKFLOW}/dispatches', dispatch,
       {'ref': os.environ.get('SELF_REF', 'main'), 'inputs': {'sha': head}})
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{self_repo}/actions/workflows/{WORKFLOW}"
    try:
        gh('POST', f'{API}/repos/{repo}/statuses/{head}', token,
           {'state': 'pending', 'context': STATUS_CONTEXT, 'description': 'queued', 'target_url': run_url})
    except (urllib.error.URLError, OSError):
        pass
    return {'dispatched': 1}


STEPS = {'prepare': cmd_prepare, 'check': cmd_check, 'build': cmd_build, 'sign': cmd_sign, 'publish': cmd_publish, 'poll': cmd_poll}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('step', choices=sorted(STEPS))
    ap.add_argument('--sha', default='')
    ap.add_argument('--src', default='')
    ap.add_argument('--spec-path', default='')
    ap.add_argument('--work', default='')
    ap.add_argument('--log', required=True)
    ap.add_argument('--result', default='')
    ap.add_argument('--run-date', default=dt.date.today().isoformat())
    a = ap.parse_args(argv)
    t0 = time.monotonic()
    log = Log(a.log)
    log.note(f'image step {a.step}')
    result = Path(a.result) if a.result else None
    try:
        if a.step != 'poll' and not SHA_RE.fullmatch(a.sha):
            raise Failure('invalid commit id')
        fields = STEPS[a.step](a, log)
        state, reason = 'success', ''
    except Failure as error:
        state, reason, fields = 'failure', str(error), {}
    except (urllib.error.URLError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        state, reason, fields = 'error', f'{type(error).__name__}: {getattr(error, "code", "")}', {}
    log.note(f'image step {a.step}: {state} {reason}')
    log.handle.close()
    if result:
        record(result, stage=a.step, state=state, reason=reason, **fields)
    secs = time.monotonic() - t0
    if a.step == 'poll':
        print(f"poll: {'error' if state != 'success' else str(fields.get('dispatched', 0)) + ' dispatched'}")
    elif a.step == 'check':
        print(f"check: {('stored' if fields.get('stored') else 'missing') if state == 'success' else state}")
    elif a.step == 'sign' and state == 'success':
        print(f"sign: pass, key {fields['key']}")
    else:
        print(f"{a.step}: {'pass' if state == 'success' else 'fail'}, {secs:.0f}s")
    return 0 if state == 'success' else 1


if __name__ == '__main__':
    sys.exit(main())
