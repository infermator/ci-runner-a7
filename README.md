# Generic CI runner

This repository runs the checks of one private repository on GitHub's free hosted runners for public repositories. It contains **no product code**: only the workflow plumbing in `.github/workflows/` and the helper scripts in `bin/`. The private repository and the log repository are named only in encrypted repository secrets.

## How it works

1. **Trigger.** `quality` runs on `workflow_dispatch` (input `sha`, 40 hex characters; input `suites`, default `quality sql`) and on a schedule every 10 minutes. The scheduled poller lists the source repository's open pull requests and dispatches a run for each head commit that has no `ci/quality` status yet (at most 3 per poll, most recently updated first), plus the head of the default branch. A `pending` status older than 6 hours counts as missing.
2. **Checkout.** Each job fetches the source at the exact commit with a read token. The token is passed as a one-shot HTTP header and never written to git config (`bin/fetch-source.sh`).
3. **Execution.** The source repository keeps its job definitions in its own workflow files. `bin/run-job.py` reads one job there and executes its shell steps in order, with the job and step environments, `if: always()` / `failure()` and step timeouts. This repository provides the toolchain: Node 24, Python 3.12 and, for the database lane, a PostgreSQL 17 service on port 55432. Any feature the runner does not support (another action, toolchain version, service, condition or expression) fails the job closed instead of running a partial copy.
4. **Results.** Commit statuses on the tested commit in the source repository: `ci/quality` (aggregate) and `ci/quality/<job>` per job. Each is `pending` at the start and `success`, `failure` or `error` at the end, and links to the run here. Re-running a commit is idempotent: the newest status per context wins.
5. **Logs.** Full job output is pushed to a private log repository as `runs/<date>/<sha>/<job>.log`, with `<job>.json` and a run `summary.json`. A weekly `prune` workflow deletes folders older than 30 days and squashes that repository's history.

## Public log policy

Logs of a public repository are public. Every step that touches source output redirects it to a file. The job log shows only fixed lines: per job `<job>: pass|fail|error, steps N/M, Ns`, and whether the log was stored and the status posted. No file paths, test names or code. There are no `pull_request` or `pull_request_target` triggers, so fork pull requests never receive secrets. The workflow has `permissions: {}`; only the poller gets `actions: write` to dispatch runs. Third-party actions are pinned to full commit SHAs, and inputs reach scripts through environment variables only.

## Configuration

Repository secrets:

| Secret | Value |
|---|---|
| `SRC_REPO` | `owner/name` of the source repository |
| `SRC_TOKEN` | fine-grained token on the source repository: Contents read, Metadata read, Commit statuses read and write, Pull requests read |
| `LOG_REPO` | `owner/name` of the private log repository |
| `LOG_TOKEN` | optional fine-grained token on the log repository only: Contents read and write. When it is not set, `SRC_TOKEN` is used and then needs that access too |

Recommended repository settings: Actions > General > "Require approval for all external contributors" for fork pull request workflows. Scheduled workflows in a public repository are paused by GitHub after 60 days without repository activity; re-enable `quality` in the Actions tab if that happens.

## Adding a job

Add a matrix entry (`label`, `workflow`, `job`) under `app` (no database) or `db` (PostgreSQL 17 service) in `.github/workflows/quality.yml`. The named job must exist in the source repository's workflow file and use only what this runner supports.
