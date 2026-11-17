# Generic CI runner

This repository runs the checks of one private repository, and builds its container images, on GitHub's free hosted runners for public repositories. It contains **no product code**: only the workflow plumbing in `.github/workflows/` and the helper scripts in `bin/`. The private repository and the log repository are named only in encrypted repository secrets.

## How it works

1. **Trigger.** `quality` runs on `workflow_dispatch` (input `sha`, 40 hex characters; input `suites`, default `quality sql`). The poller (`bin/poll.py`) runs every 4 minutes inside `pulse`, a chain of dispatched runs that does not depend on GitHub's cron: GitHub's schedule is best effort and did not fire once in this repository's first day, so pull requests waited for manual dispatches. Each `pulse` run polls for about 55 minutes and dispatches its successor; a failed hand-over is retried, `pulse-watchdog` runs after every `pulse` run and restarts the chain if that run left no successor, every `quality` run restarts a broken chain (job `keepalive`), and the schedules of `quality` and `pulse` do too whenever GitHub fires them. To stop polling, disable `pulse` in the Actions tab. The poller lists the source repository's open pull requests and dispatches a run for each head commit that has no `ci/quality` status yet (at most 3 per poll, most recently updated first), plus the head of the default branch. A `pending` status older than 6 hours counts as missing.
2. **Checkout.** Each job fetches the source at the exact commit with a read token. The token is passed as a one-shot HTTP header and never written to git config (`bin/fetch-source.sh`).
3. **Execution.** The source repository keeps its job definitions in its own workflow files. `bin/run-job.py` reads one job there and executes its shell steps in order, with the job and step environments, `if: always()` / `failure()` and step timeouts. This repository provides the toolchain: Node 24, Python 3.12 and, for the database lane, a PostgreSQL 17 service on port 55432. Any feature the runner does not support (another action, toolchain version, service, condition or expression) fails the job closed instead of running a partial copy.
4. **Results.** Commit statuses on the tested commit in the source repository: `ci/quality` (aggregate) and `ci/quality/<job>` per job. Each is `pending` at the start and `success`, `failure` or `error` at the end, and links to the run here. Re-running a commit is idempotent: the newest status per context wins.
5. **Logs.** Full job output is pushed to a private log repository as `runs/<date>/<sha>/<job>.log`, with `<job>.json` and a run `summary.json`. A weekly `prune` workflow deletes folders older than 30 days and squashes that repository's history.

## Public log policy

Logs of a public repository are public. Every step that touches source output redirects it to a file. The job log shows only fixed lines: per job `<job>: pass|fail|error, steps N/M, Ns`, and whether the log was stored and the status posted. No file paths, test names or code. There are no `pull_request` or `pull_request_target` triggers, so fork pull requests never receive secrets. The workflow has `permissions: {}`; only the poller (`poll`, `pulse`), `keepalive` and `pulse-watchdog` get `actions: write`, to dispatch runs. Third-party actions are pinned to full commit SHAs, and inputs reach scripts through environment variables only.

## Configuration

Repository secrets:

| Secret | Value |
|---|---|
| `SRC_REPO` | `owner/name` of the source repository |
| `SRC_TOKEN` | fine-grained token on the source repository: Contents read, Metadata read, Commit statuses read and write, Pull requests read |
| `LOG_REPO` | `owner/name` of the private log repository |
| `LOG_TOKEN` | optional fine-grained token on the log repository only: Contents read and write. When it is not set, `SRC_TOKEN` is used and then needs that access too |
| `ART_REPO` | `owner/name` of the private artifact repository (`image` only) |
| `ART_TOKEN` | fine-grained token on the artifact repository only: Contents read and write (`image` only; never reuse `LOG_TOKEN` or `SRC_TOKEN`) |
| `IMAGE_SIGNING_KEY` | OpenSSH ed25519 private key without passphrase; its public key is pinned by the consumer (`image` only) |
| `STAGING_PUBLIC_KEY` | the public build value substituted for `{public_key}` in the spec (`image` only) |

Repository variable `IMAGE_AUTO` = `true` turns on the scheduled default-branch build of `image`.

Recommended repository settings: Actions > General > "Require approval for all external contributors" for fork pull request workflows. Scheduled workflows in a public repository are paused by GitHub after 60 days without repository activity; re-enable `quality` in the Actions tab if that happens. `pulse` runs keep the repository active, but a chain stopped by hand stays stopped until a `quality` run or a manual `pulse` dispatch restarts it.

## Image builds (`image`)

`image` builds one source commit into a container image and stores it **privately**: never as a workflow artifact, a package or anything else this public repository can expose.

1. **Trigger.** `workflow_dispatch` with `sha` (40 hex characters) and optional `force` (rebuild an image that is already stored; a rebuild has a new image id). A schedule every 20 minutes dispatches the head of the source default branch when it has no `ci/image` status yet; it is off unless the repository variable `IMAGE_AUTO` is `true`.
2. **Spec.** The source repository describes the image in a JSON file at the commit (`SPEC_PATH` in the workflow; the default-branch head's copy is used when the commit predates it): image tag, platform, excluded paths, build arguments (`{public_key}` is replaced by the `STAGING_PUBLIC_KEY` secret), boundary checks, fixed manifest fields, signature namespace and header, release name and how many builds to keep. `bin/image.py` validates it and fails closed on anything unknown. The spec is data: no source code runs on the runner host.
3. **Build.** `git archive` of the exact commit (minus the excluded paths), `docker buildx build --platform <p> --provenance=false` with the revision label, in the classic image store (switched on if the runner's daemon uses the containerd store). Build argument values reach docker through its environment, never an argument list.
4. **Checks.** Revision label and platform of the image; then a container without network checks the runtime user, the required files, `node --check` and that no server-only name from the spec occurs in the browser files.
5. **Archive.** `docker save | gzip -1`, its SHA-256, and a check that the archive holds exactly this image (tag and config digest). A manifest JSON records commit, image id, tag, archive digest, the spec's fixed fields and the build provenance (this run and the private log path).
6. **Signature.** `ssh-keygen -Y sign` with the builder key (`IMAGE_SIGNING_KEY`) over `<header>\nrelease_sha: ..\nimage_id: ..\narchive_sha256: ..\n` in the spec's namespace, verified once before upload. The key exists only in this step, written to a 0600 file that is deleted before the step ends; the log shows its public fingerprint.
7. **Store.** The archive, manifest and signature are uploaded as `<prefix><sha>.tar.gz`, `.json`, `.sig` to a prerelease of the private artifact repository (`ART_REPO`), replacing assets of the same name; older builds beyond the spec's `keep` are deleted.
8. **Report.** Full log and result to the private log repository (`runs/<date>/<sha>/image.log`, `image.json`), commit status `ci/image` on the source commit (`stored <image id>`, `already stored`, `failed at <step>`).

**Trust.** Whoever can run workflows of this repository with its secrets can sign images: today that is only the owner account (no pull request triggers, no other collaborators). A consumer that pins the builder public key accepts any image this key signed, so it should also require its own approval of the exact image id. Rotate the key by replacing the secret and the pinned public key together.

Why an artifact repository and not a container registry: a package pushed from a public repository's workflow can end up linked to it and public, and a registry pull does not yield the `docker save` archive whose digest is signed. Release assets of a private repository are readable only with a token on that repository; `ART_TOKEN` can write there and nowhere else, and it is used only by `image`, whose host never runs source code (the test jobs of `quality` run source code on their host, so their tokens must be assumed readable by it).

## Self-test (`selftest`)

On every push to this repository: shell and Python syntax, workflow YAML, `tests/test_image.py`, and `tests/image-e2e.sh`, which runs the whole image pipeline (prepare, build, checks, archive, signature) on a synthetic source with a throw-away key and checks that a forbidden name in the browser files fails the build. No secrets, no source repository.

## Adding a job

Add a matrix entry (`label`, `workflow`, `job`) under `app` (no database) or `db` (PostgreSQL 17 service) in `.github/workflows/quality.yml`. The named job must exist in the source repository's workflow file and use only what this runner supports.
