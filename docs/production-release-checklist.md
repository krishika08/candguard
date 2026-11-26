# CandGuard v0.2.0 — Release Checklist (planned, not yet released)

This checklist covers publishing **v0.2.0** to PyPI through the hardened `.github/workflows/publish.yml` workflow. v0.2.0 has **not** been released. The latest published version is 0.1.0. Once the version-bump PR (section 1) is merged, the package on `main` declares `0.2.0`.

CandGuard is already on PyPI, so this is not a first-time publication. Do not create a new "pending publisher". Verify the existing Trusted Publishing configuration instead (section 2).

Distributions are only ever uploaded by the workflow. Do not build and upload them manually with `twine upload`.

---

## 1. Version bump (separate, explicitly approved PR)

The release workflow refuses to publish unless the tag and **both** version declarations match exactly. Before tagging, a separate, explicitly approved pull request must update:

| File | Field | Value for this release |
|:--|:--|:--|
| `pyproject.toml` | `[project].version` | `0.2.0` |
| `src/candguard/__init__.py` | `__version__` | `0.2.0` |

The release tag must then be `v0.2.0`.

That PR should also move the [`CHANGELOG.md`](../CHANGELOG.md) entries from `[Unreleased]` to a `[0.2.0]` heading. Add the release date there, or right after publication.

The same PR must also put `README.md` in its **final form for the release**. `pyproject.toml` declares `readme = "README.md"`, so the build embeds the README in the wheel and sdist metadata, and PyPI shows it as the 0.2.0 project description. A published release's description cannot be changed afterwards. Before tagging, the README must describe 0.2.0 features as part of 0.2.0 (not as unreleased or development-only) and must not claim that 0.2.0 is already on PyPI.

- [ ] The version-bump PR is reviewed, CI is green, and it is merged into `main`.
- [ ] `pyproject.toml` and `src/candguard/__init__.py` on `main` both declare `0.2.0`.
- [ ] `README.md` on `main` is the intended 0.2.0 project description.

---

## 2. Verify Trusted Publishing and the `pypi` environment

The publish job runs in the GitHub environment `pypi` and authenticates to PyPI with OIDC (`id-token: write`). It uses no stored API token. Check the existing configuration rather than assuming it.

### PyPI Trusted Publisher

Open the project's publishing settings: <https://pypi.org/manage/project/candguard/settings/publishing/>. You need owner or maintainer access, with two-factor authentication enabled.

- [ ] A GitHub Trusted Publisher exists for the `candguard` project.
- [ ] **Owner** is `krishika08`.
- [ ] **Repository name** is `candguard`.
- [ ] **Workflow name** is `publish.yml`, the file name only, matching `.github/workflows/publish.yml`.
- [ ] **Environment name** is `pypi`, matching `environment: name: pypi` in the publish job.

If any value differs, correct the PyPI configuration before tagging. A mismatch makes the upload step fail with a Trusted Publishing error, after every validation job has already passed.

### GitHub `pypi` environment

Open <https://github.com/krishika08/candguard/settings/environments> and select `pypi`.

- [ ] The environment exists and is named exactly `pypi`.
- [ ] Its deployment branch and tag rules allow the new tag. If they were restricted to `v0.1.0` for the first release, add `v0.2.0` or a `v*` tag rule; otherwise the publish job cannot start. With **Selected branches and tags**, only refs that match a rule can deploy, so a selection with no rules at all also blocks the release.
- [ ] If **required reviewers** are configured, a reviewer is available to approve the deployment while the workflow runs.

---

## 3. Pre-tag validation

On an up-to-date checkout of `main` that contains the version bump:

```bash
git switch main
git pull --ff-only origin main

python -m venv .venv
# macOS/Linux: source .venv/bin/activate      Windows: .venv\Scripts\Activate.ps1

# Default suite (Parquet tests are skipped without PyArrow)
pip install -e ".[dev]"
pytest

# Optional Parquet suite
pip install -e ".[dev,parquet]"
pytest
```

Optionally, check that the distributions build:

```bash
pip install build twine
python -m build
python -m twine check dist/*
```

These checks are local preparation only. The release workflow builds its own distributions, and those are the only files uploaded to PyPI. It also re-runs the full validation itself (section 5).

- [ ] Both local test runs pass.
- [ ] The latest `tests` workflow run on `main` succeeded. Check with `gh run list --repo krishika08/candguard --branch main --workflow tests.yml --limit 3`.

---

## 4. Tag and publish

1. **Merge the approved version-bump PR** (section 1).
2. **Confirm `main` is up to date and CI passes**: `git pull --ff-only origin main`, then check the latest `tests` run (section 3).
3. **Confirm the tag and both version declarations match:**
   ```bash
   grep -n '^version' pyproject.toml                   # version = "0.2.0"
   grep -n '^__version__' src/candguard/__init__.py    # __version__ = "0.2.0"
   ```
4. **Create and push an annotated stable release tag** on the `main` commit that contains the bump:
   ```bash
   git tag -a v0.2.0 -m "CandGuard v0.2.0"
   git push origin v0.2.0
   ```
   Only tags in the strict `vMAJOR.MINOR.PATCH` format are accepted, for example `v0.2.0`. Leading zeros (`v0.02.0`), pre-release suffixes (`v0.2.0rc1`, `v0.2.0-rc.1`), build suffixes (`v0.2.0+build1`) and missing components (`v0.2`) are rejected by `validate-version`, before anything is tested, built or uploaded.
5. **Monitor the `publish.yml` workflow:**
   ```bash
   gh run list --repo krishika08/candguard --workflow publish.yml --limit 3
   gh run watch --repo krishika08/candguard <run-id>
   ```
   If the `pypi` environment requires reviewers, approve the `publish-to-pypi` deployment when prompted.
6. **Verify the published distribution on PyPI** (section 6).
7. **Test installation from a clean environment** (section 6).
8. **Create the GitHub release** only after publication succeeded and was verified (section 6).

---

## 5. What the release workflow does

`publish.yml` runs **only** when a tag matching `v*` is pushed. Branch pushes and pull requests run the `tests` workflow only; they never execute the publishing workflow.

| Job | Runs after | Purpose |
|:--|:--|:--|
| `validate-version` | none (first job) | On Python 3.12 using only the standard library, requires the tag to be `vMAJOR.MINOR.PATCH` and equal to `pyproject.toml` `[project].version` and `candguard.__version__`. On any mismatch it fails with a `Version mismatch` or `Malformed release tag` error, and every later job is skipped. |
| `test` | `validate-version` | Full default test suite (`pytest --cov=candguard`) on Ubuntu Python 3.10, 3.11, 3.12 and 3.13, Windows 3.12 and macOS 3.12. Also checks that the default install does not install PyArrow. |
| `test-parquet` | `validate-version` | Full suite with the optional `parquet` extra on Ubuntu Python 3.10 (PyArrow must resolve to major version 25), Ubuntu 3.13, Windows 3.12 and macOS 3.12. |
| `cli-smoke` | `validate-version` | Runs the `candguard` CLI. Checks the basic audit, the recall gate (passing and failing), JSON output, the `compare` recall-drop and pair-growth gates (passing and failing), and candidate-set drift with `--min-candidate-jaccard 1.0`. |
| `build-distributions` | `validate-version`, `test`, `test-parquet`, `cli-smoke` | Builds the wheel and sdist, runs `twine check`, and requires the file names to match the tag version. It then installs the wheel in a clean virtual environment, outside the checkout, and verifies the installed version, `candguard --version`, `--help`, that no PyArrow or other default dependency is installed, and a basic audit. Only then does it upload the `candguard-dist` artifact. |
| `publish-to-pypi` | all five jobs above | Downloads `candguard-dist` and uploads it with `pypa/gh-action-pypi-publish` through Trusted Publishing (OIDC). |

- **Publication gate:** `publish-to-pypi` runs only if every required job **succeeded**. A failed, cancelled or skipped job, including any single matrix entry, prevents publication.
- **Permissions:** the workflow is read-only (`contents: read`). Only `publish-to-pypi` receives `id-token: write`, and it runs in the `pypi` environment.

---

## 6. Post-publication verification

1. **PyPI project page:** open <https://pypi.org/project/candguard/0.2.0/>.
   - [ ] Version `0.2.0` is listed, and both the `.whl` and the `.tar.gz` appear under "Download files".
   - [ ] The description, license (MIT), project links and classifiers are correct, and `Requires-Python` is `>=3.10`.
2. **Clean-environment installation:**
   ```bash
   python -m venv candguard-verify
   # macOS/Linux: source candguard-verify/bin/activate      Windows: candguard-verify\Scripts\Activate.ps1
   pip install --no-cache-dir candguard==0.2.0

   candguard --version        # expected: candguard 0.2.0
   candguard --help
   python -c "import importlib.util; print('pyarrow installed:', importlib.util.find_spec('pyarrow') is not None)"   # expected: False
   ```
3. **Basic audit smoke test.** The example files are not shipped in the package, so run this from a checkout, or copy `examples/` first:
   ```bash
   candguard audit --candidates examples/candidates.tsv --truth examples/truth.tsv \
     --left-id source_id --right-id candidate_id --min-recall 0.60
   # expected: exit code 0 and "Quality gate: PASS"
   ```
4. **Optional extra:** in the same environment, `pip install "candguard[parquet]==0.2.0"` should install PyArrow.
5. **GitHub release** (after the checks above pass). Create it from the tag, with notes taken from the `0.2.0` section of `CHANGELOG.md`. If you attach files, attach the exact distributions the workflow published (download them with `gh run download <run-id> --name candguard-dist`). Do not attach a local rebuild.
   ```bash
   gh release create v0.2.0 --repo krishika08/candguard --title "CandGuard v0.2.0" --notes-file <notes.md>
   ```
6. **Follow-up documentation PR:**
   - `README.md` needs no release update here: it was finalized in the version-bump PR (section 1) because it is the published project description;
   - in `SECURITY.md`, add 0.2.x to the supported-versions table and decide the status of 0.1.x;
   - in `CHANGELOG.md`, confirm the release date.

---

## 7. If the release workflow fails

Investigate and fix the cause before trying another release. Never upload distributions manually.

### 7.1 A failed publish job does not mean nothing was published

PyPI publication can **partially succeed**. `publish-to-pypi` uploads the wheel and the source distribution one at a time, so a job can fail after one file is already on PyPI. For example, the wheel uploads and then the sdist upload fails. PyPI never allows a file name to be uploaded twice, even after the file is deleted, so a version with any uploaded file can never be completed or replaced.

So a red or failed `publish-to-pypi` job must **not** be read as "nothing was published". Before deleting or recreating any release tag, and before re-running `publish-to-pypi`, check what PyPI actually has for the target version. Re-running the publish job is itself an upload attempt, and after a partial upload it could add the missing file under a version that is already consumed.

### 7.2 Check the target version on PyPI

Use the version being released, here `0.2.0`:

```bash
# 404 = PyPI has no release for this version; 200 = the version exists
curl -s -o /dev/null -w "%{http_code}\n" https://pypi.org/pypi/candguard/0.2.0/json

# If 200: list every file published for the version (yanked files are listed too)
curl -s https://pypi.org/pypi/candguard/0.2.0/json \
  | python -c "import json, sys; [print(f['packagetype'], f['filename'], 'yanked' if f['yanked'] else '') for f in json.load(sys.stdin)['urls']]"
```

`bdist_wheel` is the wheel (`candguard-0.2.0-py3-none-any.whl`), and `sdist` is the source distribution (`candguard-0.2.0.tar.gz`). Also check the files listed at <https://pypi.org/project/candguard/0.2.0/#files>.

The JSON API and the files page show only what PyPI lists **now**. A 404 from the latest check does not prove that no upload ever happened for this version, so also check the workflow history below.

#### Inspect every publish attempt for the target tag

An earlier run, or an earlier attempt of the same run, can have uploaded a file even if the latest attempt shows no upload. Inspect **all** available `publish.yml` runs and attempts for the exact tag, not just the latest failed one. This includes runs from a tag that was deleted and pushed again, which can point at different commits.

```bash
# Every publish.yml run for the tag (tag pushes are listed under the tag name).
# --all includes runs of the workflow while it was disabled.
gh run list --repo krishika08/candguard --workflow publish.yml --branch v0.2.0 --all --limit 1000 \
  --json databaseId,attempt,headSha,status,conclusion,createdAt

# For each run ID, check every attempt from 1 up to the "attempt" value above
gh run view <run-id> --repo krishika08/candguard --attempt <n> --log \
  | grep -E "Uploading (distributions to|candguard-)|View at|already exists|HTTPError"

# Every deployment record for the pypi environment and the tag. --paginate fetches all pages,
# so older records cannot be silently omitted.
gh api --paginate "repos/krishika08/candguard/deployments?environment=pypi&per_page=100" \
  --jq '.[] | select(.ref == "v0.2.0") | "\(.id) \(.sha) \(.created_at)"'

# For each deployment ID: its status history; log_url identifies the workflow run and job
gh api --paginate "repos/krishika08/candguard/deployments/<deployment-id>/statuses" \
  --jq '.[] | "\(.state) \(.created_at) \(.log_url)"'
```

**Incomplete run history:** `gh run list` returns at most `--limit` runs. If it returns exactly 1000 runs, the history may be truncated. Split it into narrower creation-date ranges, for example `--created "2026-11-01..2026-11-15"`, until every range returns fewer runs than the limit and the ranges together cover the whole period since the tag could first have been pushed. Incomplete history is never proof that no upload occurred.

**Deployment records:** a deployment record identifies a `publish-to-pypi` job that targeted the `pypi` environment. It does not prove the job started. The record is created while the job is still waiting, for example for a required reviewer to approve the environment, and its statuses typically progress through `waiting`, `queued`, `in_progress` and then `success`, `failure` or `error`. For every deployment, follow its `log_url` to the run and job:
- check the job's final status;
- if the job ran, inspect its log with the commands above.

A deployment still `waiting` or `queued` can still be approved and then upload. Resolve it (let it finish, or reject or cancel it) and re-check before classifying the version.

In the `publish-to-pypi` log, `Uploading distributions to https://upload.pypi.org/legacy/` means an upload was attempted. `Uploading candguard-0.2.0-py3-none-any.whl` or `Uploading candguard-0.2.0.tar.gz` means that file's upload **started**; it does not prove the upload completed or failed. `View at:` follows a completed upload. (`Uploading artifact: candguard-dist.zip` is the GitHub Actions artifact step, not PyPI.) Any of these lines in any attempt is upload evidence.

A missing, expired, inaccessible or inconclusive log is **never** proof that no upload occurred. GitHub deletes logs after the repository's retention period. A log that cannot be read, or a `pypi` deployment whose job log is gone, leaves that attempt unexplained.

#### Classify the result

The version has **no upload** only when **all** of these hold:

1. The JSON API consistently returns 404. Recheck after allowing a few minutes for propagation, not just once.
2. The PyPI files page lists no distribution for the version.
3. Every available `publish.yml` run and attempt for the exact tag has been inspected, and every `pypi` deployment for the tag is accounted for.
4. No log shows any upload evidence, and there is no known prior publication or deletion of any file for this version.
5. All of these sources agree with each other.

Treat the status as **uncertain** if any condition fails or cannot be checked. For example:
- any attempt shows upload evidence, even if PyPI now returns 404;
- a log is missing, expired, unreadable or inconclusive;
- the run history may be incomplete, for example a run list reached its limit and was not split into narrower date ranges;
- a `pypi` deployment is still `waiting` or `queued`, or its workflow run and job cannot be identified;
- a file is known to have been deleted from PyPI (deleted files no longer appear, but their names can never be reused);
- the sources disagree, or PyPI returns errors.

### 7.3 Decide using this table

| PyPI state for the target version | Allowed action |
|:--|:--|
| Neither distribution exists, **fully confirmed**: all five "no upload" conditions in section 7.2 hold | Correct the problem. Tag recovery may be considered (section 7.4). |
| Only the wheel exists | The version is consumed: use a new version (section 7.5). |
| Only the sdist exists | The version is consumed: use a new version (section 7.5). |
| Both exist | Publication succeeded. Do the post-publication verification (section 6). |
| Status uncertain: any "no upload" condition fails or cannot be checked. This includes upload evidence in any earlier attempt, a missing or inconclusive log, a known deletion, or conflicting sources, even when PyPI returns 404 | Stop and investigate. Do not delete tags, re-run `publish-to-pypi` or bump the version until the state is known. Tag recovery is **not** permitted. |

A 404 from the latest JSON API check is never enough on its own to choose the first row.

If **either** the wheel or the sdist exists for a version, that version is consumed. Never attempt to upload the missing file, whether with `twine upload`, by re-running `publish-to-pypi`, or by pushing the same tag again. Never reuse the version number.

### 7.4 Tag recovery (only when neither distribution was uploaded)

Delete and recreate a release tag **only** after section 7.2 has confirmed that neither the wheel nor the sdist was uploaded for that version.

1. Read the failing job's log. For `validate-version`, compare the tag with both version declarations.
2. If the failure was transient (for example a runner or network problem) in a job **before** `publish-to-pypi`, and the code is correct, re-run the failed jobs: `gh run rerun <run-id> --failed`. If `publish-to-pypi` itself failed, re-run it only after section 7.2 confirms that no file was uploaded.
3. If a code, version or configuration change is needed, fix it through a normal reviewed PR into `main`. Then delete the tag (`git push --delete origin v0.2.0` and `git tag -d v0.2.0`) and create it again on the corrected commit (section 4).

### 7.5 A consumed version: release a new patch version

When any file exists for the version (wheel only, sdist only, or both with a later problem found):

1. Do not delete or recreate the version's tag, do not re-run `publish-to-pypi` for it, and do not upload the missing file.
2. Fix the underlying problem through a normal reviewed PR into `main`.
3. Prepare the next patch version, for example `0.2.1`, through a **separate, explicitly approved version-bump PR**. It updates both `pyproject.toml` (`[project].version`) and `src/candguard/__init__.py` (`__version__`), exactly as in section 1. Move the `CHANGELOG.md` entries to the new version.
4. Release the new version with a new tag (`v0.2.1`) through section 4. The workflow's `validate-version` job requires the tag and both declarations to match.
5. If the consumed version is incomplete, for example only the wheel was uploaded, or it is harmful, consider yanking it on PyPI so installers skip it by default. Yanking does not make the version reusable.

### 7.6 Trusted Publishing errors

If `publish-to-pypi` fails with a Trusted Publishing or OIDC error, re-check section 2 (owner, repository, workflow file name and environment name). Then still run the section 7.2 check before re-running the job or touching the tag.
