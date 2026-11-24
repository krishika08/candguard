# CandGuard v0.2.0 — Release Checklist (planned, not yet released)

This checklist covers publishing **v0.2.0** to PyPI through the hardened `.github/workflows/publish.yml` workflow. v0.2.0 has **not** been released. The latest published version is 0.1.0, and the package on `main` still declares `0.1.0`.

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

- [ ] The version-bump PR is reviewed, CI is green, and it is merged into `main`.
- [ ] `pyproject.toml` and `src/candguard/__init__.py` on `main` both declare `0.2.0`.

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
- [ ] Its deployment branch and tag rules allow the new tag. If they were restricted to `v0.1.0` for the first release, add `v0.2.0` or a `v*` tag rule; otherwise the publish job cannot start.
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
   - in `README.md`, update the "Project status" and installation sections for the new published version;
   - in `SECURITY.md`, add 0.2.x to the supported-versions table and decide the status of 0.1.x;
   - in `CHANGELOG.md`, confirm the release date.

---

## 7. If the release workflow fails

Investigate and fix the cause before trying another release. Never upload distributions manually.

- **Nothing was published** (`publish-to-pypi` was skipped or failed before uploading):
  1. Read the failing job's log. For `validate-version`, compare the tag with both version declarations.
  2. If the failure was transient (for example a runner or network problem) and the code is correct, re-run the failed jobs: `gh run rerun <run-id> --failed`.
  3. If a code, version or configuration change is needed, fix it through a normal reviewed PR into `main`. Then delete the tag (`git push --delete origin v0.2.0` and `git tag -d v0.2.0`) and create it again on the corrected commit (section 4). This is safe only because nothing was uploaded under that version.
- **The upload to PyPI succeeded, but a later problem is found:** a version and its files cannot be re-uploaded or replaced on PyPI. Do not reuse that version number, and do not delete and recreate its tag. Fix the problem in a PR, bump to the next patch version (for example `0.2.1`), and release that. If the published version is harmful, consider yanking it on PyPI.
- **Trusted Publishing errors in `publish-to-pypi`:** re-check section 2 (owner, repository, workflow file name and environment name) before re-running.
