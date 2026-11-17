#!/usr/bin/env python3
"""Run the `run:` steps of one job of a workflow file found in the checked-out source tree.

The source repository keeps the single definition of its checks in its own workflow files. This runner provides the
toolchain (Node, Python, an optional PostgreSQL service) and then executes that job's shell steps in order, with the
job and step environments, `if: always()` / `failure()` handling and step timeouts.

Privacy: every byte of step output goes to --log. Standard output carries exactly one summary line. Unknown workflow
features fail closed, with the reason written to --log only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback

import yaml

EXPR = re.compile(r'\$\{\{\s*(.*?)\s*\}\}')
ALLOWED_ACTIONS = ('actions/checkout@', 'actions/setup-node@', 'actions/setup-python@')
SAFE_NAME = re.compile(r'^[A-Za-z0-9_.-]+$')


class Unsupported(Exception):
    pass


def substitute(value, ctx: dict[str, str]):
    if not isinstance(value, str):
        return value

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in ctx:
            raise Unsupported(f'expression not supported by this runner: {key}')
        return ctx[key]
    return EXPR.sub(repl, value)


def env_block(block, ctx) -> dict[str, str]:
    if not block:
        return {}
    if not isinstance(block, dict):
        raise Unsupported('env block is not a mapping')
    out = {}
    for k, v in block.items():
        if isinstance(v, bool):
            v = 'true' if v else 'false'
        out[str(k)] = substitute(str(v), ctx)
    return out


def read_env_file(path: str) -> dict[str, str]:
    """GITHUB_ENV format: NAME=value lines and NAME<<DELIM heredocs."""
    out: dict[str, str] = {}
    try:
        lines = open(path, encoding='utf-8', errors='replace').read().split('\n')
    except FileNotFoundError:
        return out
    i = 0
    while i < len(lines):
        line = lines[i]
        if '<<' in line and ('=' not in line or line.index('<<') < line.index('=')):
            name, delim = line.split('<<', 1)
            body = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            out[name] = '\n'.join(body)
        elif '=' in line:
            name, value = line.split('=', 1)
            out[name] = value
        i += 1
    return out


def check_toolchain(step: dict, node: str, python: str) -> None:
    uses = step['uses']
    if not uses.startswith(ALLOWED_ACTIONS):
        raise Unsupported(f'action not provided by this runner: {uses}')
    w = step.get('with') or {}
    if uses.startswith('actions/setup-node@'):
        want = str(w.get('node-version', node))
        if want.split('.')[0].lstrip('v') != node:
            raise Unsupported(f'job wants Node {want}, runner provides {node}')
    if uses.startswith('actions/setup-python@'):
        want = str(w.get('python-version', python))
        if want != python:
            raise Unsupported(f'job wants Python {want}, runner provides {python}')


def check_services(job: dict, pg_image: str) -> None:
    services = job.get('services') or {}
    if not services and not pg_image:
        return
    if not pg_image:
        raise Unsupported('job declares services; run it on the database lane')
    if set(services) != {'postgres'}:
        raise Unsupported(f'job services {sorted(services)} differ from the provided postgres service')
    svc = services['postgres']
    if svc.get('image') != pg_image:
        raise Unsupported(f'job wants image {svc.get("image")}, runner provides {pg_image}')
    if [str(p) for p in svc.get('ports') or []] != ['55432:5432']:
        raise Unsupported('job wants different postgres ports than 55432:5432')


def should_run(cond, failed: bool) -> bool:
    if cond is None:
        return not failed
    c = str(cond).strip()
    if c.startswith('${{') and c.endswith('}}'):
        c = c[3:-2].strip()
    if c == 'always()':
        return True
    if c == 'success()':
        return not failed
    if c == 'failure()':
        return failed
    raise Unsupported(f'step condition not supported by this runner: {c}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--workflow', required=True)
    ap.add_argument('--job', required=True)
    ap.add_argument('--label', required=True)
    ap.add_argument('--src', required=True)
    ap.add_argument('--log', required=True)
    ap.add_argument('--result', required=True)
    ap.add_argument('--sha', required=True)
    ap.add_argument('--node', required=True)
    ap.add_argument('--python', required=True)
    ap.add_argument('--pg-image', default='')
    ap.add_argument('--pg-container', default='')
    a = ap.parse_args()

    t0 = time.monotonic()
    total = ok = 0
    failed = False
    log = open(a.log, 'a', encoding='utf-8', errors='replace')

    def note(msg: str) -> None:
        log.write(f'\n##### {msg}\n')
        log.flush()

    try:
        if not SAFE_NAME.match(a.workflow) or not SAFE_NAME.match(a.job):
            raise Unsupported('workflow or job name has unexpected characters')
        path = os.path.join(a.src, '.github', 'workflows', a.workflow)
        with open(path, encoding='utf-8') as fh:
            doc = yaml.safe_load(fh)
        job = (doc.get('jobs') or {}).get(a.job)
        if not isinstance(job, dict):
            raise Unsupported(f'job {a.job} not found in {a.workflow}')
        check_services(job, a.pg_image)
        steps = job.get('steps') or []

        private = tempfile.mkdtemp(prefix='job-', dir=os.environ.get('RUNNER_TEMP'))
        files = {k: os.path.join(private, k.lower()) for k in ('GITHUB_STEP_SUMMARY', 'GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_PATH', 'GITHUB_STATE')}
        ctx = {
            'github.workspace': a.src,
            'github.sha': a.sha,
            'runner.temp': os.environ.get('RUNNER_TEMP', private),
            'runner.os': 'Linux',
            'job.services.postgres.id': a.pg_container,
        }
        base = dict(os.environ)
        base.update({'GITHUB_WORKSPACE': a.src, 'GITHUB_SHA': a.sha, 'CI': 'true'})
        base.update(files)
        base.update(env_block(doc.get('env'), ctx))
        base.update(env_block(job.get('env'), ctx))
        added_env: dict[str, str] = {}
        added_path: list[str] = []

        # Validate every step before running any, so an unsupported feature never leaves a half-run job.
        plan = []
        for i, step in enumerate(steps, 1):
            if 'uses' in step:
                check_toolchain(step, a.node, a.python)
                continue
            if 'run' not in step:
                raise Unsupported(f'step {i} has neither run nor uses')
            if step.get('shell') not in (None, 'bash'):
                raise Unsupported(f'step {i} uses shell {step.get("shell")}')
            should_run(step.get('if'), False)
            plan.append((i, step))

        for i, step in plan:
            if not should_run(step.get('if'), failed):
                note(f'step {i} skipped')
                continue
            total += 1
            env = dict(base)
            env.update(added_env)
            if added_path:
                env['PATH'] = os.pathsep.join(added_path + [env.get('PATH', '')])
            env.update(env_block(step.get('env'), ctx))
            script = substitute(step['run'], ctx)
            cwd = os.path.join(a.src, substitute(step.get('working-directory') or '.', ctx))
            for f in files.values():
                open(f, 'a').close()
            with tempfile.NamedTemporaryFile('w', suffix='.sh', dir=private, delete=False) as sf:
                sf.write(script)
            timeout = step.get('timeout-minutes')
            label = step.get('name') or script.strip().split('\n')[0][:120]
            note(f'step {i}: {label}')
            s0 = time.monotonic()
            try:
                rc = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', sf.name], cwd=cwd, env=env,
                                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    timeout=float(timeout) * 60 if timeout else None).returncode
            except subprocess.TimeoutExpired:
                rc = 124
            note(f'step {i} exit {rc} after {time.monotonic() - s0:.1f}s')
            added_env.update(read_env_file(files['GITHUB_ENV']))
            open(files['GITHUB_ENV'], 'w').close()
            try:
                added_path = [p for p in open(files['GITHUB_PATH']).read().split('\n') if p] + added_path
            except FileNotFoundError:
                pass
            open(files['GITHUB_PATH'], 'w').close()
            if rc == 0 or step.get('continue-on-error') is True:
                ok += 1
            else:
                failed = True
        try:
            summary = open(files['GITHUB_STEP_SUMMARY'], encoding='utf-8', errors='replace').read()
            if summary.strip():
                note('job summary')
                log.write(summary)
        except FileNotFoundError:
            pass
        state = 'failure' if failed else 'success'
        reason = ''
    except Unsupported as e:
        note(f'runner refused: {e}')
        state, reason = 'error', 'unsupported workflow feature (see log)'
    except Exception:
        note('runner error')
        log.write(traceback.format_exc())
        state, reason = 'error', 'runner error (see log)'
    finally:
        log.close()

    secs = time.monotonic() - t0
    result = {'label': a.label, 'state': state, 'steps_ok': ok, 'steps_run': total, 'seconds': round(secs, 1), 'reason': reason}
    with open(a.result, 'w') as fh:
        json.dump(result, fh)
    word = {'success': 'pass', 'failure': 'fail', 'error': 'error'}[state]
    print(f'{a.label}: {word}, steps {ok}/{total}, {secs:.0f}s')
    return 0 if state == 'success' else 1


if __name__ == '__main__':
    sys.exit(main())
