# CandGuard v0.1.0 — Production Release Checklist

This guide documents the step-by-step procedure for publishing the first stable release of `candguard` (version `0.1.0`) to production PyPI using **PyPI Trusted Publishing** via GitHub Actions.

---

## 1. Prerequisites: PyPI Account Setup

Before initiating a release, ensure your PyPI account is properly configured:

1. **Log in or Register on PyPI:**
   - URL: [https://pypi.org/](https://pypi.org/)
2. **Verify Primary Email:**
   - Confirm your email address is verified under [Account Settings](https://pypi.org/manage/account/).
3. **Enable Two-Factor Authentication (2FA):**
   - PyPI mandates 2FA (TOTP authenticator app or WebAuthn / security key) for all accounts that publish packages.
   - Configure this under [Account Security](https://pypi.org/manage/account/two-factor/).

---

## 2. Configure PyPI Pending Trusted Publisher

Because `candguard` has not yet been published to production PyPI, you must configure a **Pending Publisher** on PyPI. Once the first release succeeds, PyPI automatically transitions the project from "pending" to active project-level publisher settings.

1. Navigate to: [https://pypi.org/manage/account/publishing/](https://pypi.org/manage/account/publishing/)
2. Scroll to the section titled **"Add a pending publisher"**.
3. Select **GitHub** as the publisher.
4. Enter the exact configuration parameters:
   - **PyPI Project Name:** `candguard`
   - **Owner:** `krishika08`
   - **Repository name:** `candguard`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`
5. Click **Add**.

> [!NOTE]
> PyPI pending publishers remain valid for 14 days or until the first package upload is completed through the configured GitHub Actions workflow.

---

## 3. Configure GitHub Repository Environment

The `publish.yml` workflow targets a protected GitHub deployment environment named `pypi`.

1. Go to your repository settings:
   `https://github.com/krishika08/candguard/settings/environments`
2. Click **New environment**.
3. Name the environment: `pypi` (must match case exactly).
4. Configure Deployment protection rules:
   - Under **Deployment branches and tags**, select **Selected tags**.
   - Add the tag pattern: `v*` (or specifically `v0.1.0`).
   - *(Optional but recommended)*: Enable **Required reviewers** to require manual sign-off before artifacts are pushed to PyPI.
5. Click **Save protection rules**.

---

## 4. Local Pre-Release Sanity Review

Before tagging and pushing a release, verify the local distribution artifacts:

```powershell
# Navigate to release checkout
cd F:\candguard-public-release

# Run full test suite with coverage
python -m pytest --cov=candguard

# Clean and build distributions
Remove-Item -Recurse -Force dist, build, src/*.egg-info -ErrorAction SilentlyContinue
python -m build

# Validate distributions with Twine
python -m twine check dist/*
```

Expected artifacts in `dist/`:
- `candguard-0.1.0-py3-none-any.whl` (SHA-256: `0a78648c1678a15f98f2309a95c0d4b525843b13235998766f72b0f0710e8737`)
- `candguard-0.1.0.tar.gz` (SHA-256: `48102ce0bfbea68bc91593aef234071f19115ac6b49c74fe8386fa10077a9b58`)

---

## 5. Publishing Version 0.1.0

Follow this exact sequence to publish:

### Step 5.1 — Commit and Push Release Preparation to `main`
```bash
git add pyproject.toml src/candguard/__init__.py .github/workflows/publish.yml docs/production-release-checklist.md
git commit -m "CandGuard v0.1.0: first stable release preparation"
git push origin main
```

### Step 5.2 — Verify CI on `main`
Confirm that the existing automated tests pass on `main`:
```bash
gh run list --repo krishika08/candguard
```
Wait for all 6 matrix jobs in `tests.yml` to succeed.

### Step 5.3 — Create and Push the Release Tag
Publishing is triggered exclusively by pushing a tag matching `v*`:
```bash
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
```

### Step 5.4 — Monitor the Workflow Execution
Track the publishing workflow run:
```bash
gh run list --repo krishika08/candguard --workflow=publish.yml
gh run watch --repo krishika08/candguard
```

The workflow will:
1. Check out the tagged source.
2. Run the test suite and CLI smoke tests.
3. Build the wheel and sdist.
4. Pass artifacts to the `publish-to-pypi` job.
5. Request an OIDC token from GitHub Actions and exchange it for a temporary PyPI upload token via PyPI Trusted Publishing.
6. Upload the distributions to production PyPI.

---

## 6. Post-Publication Verification

Once GitHub Actions reports a successful run:

1. **Verify Live PyPI Page:**
   Visit: [https://pypi.org/project/candguard/0.1.0/](https://pypi.org/project/candguard/0.1.0/)
   - Confirm metadata, project description, license (MIT), GitHub links, and classifiers.
   - Confirm both `.whl` and `.tar.gz` are listed under "Download files".

2. **Clean-Environment Installation Test:**
   Test installation from production PyPI in an isolated virtual environment:
   ```bash
   python -m venv candguard-pypi-verify
   # Windows:
   candguard-pypi-verify\Scripts\Activate.ps1
   # macOS/Linux:
   # source candguard-pypi-verify/bin/activate

   # Install from PyPI
   pip install --no-cache-dir candguard==0.1.0

   # Verify CLI entry point and version
   candguard --version
   candguard --help

   # Verify CLI smoke test
   candguard audit --candidates examples/candidates.tsv --truth examples/truth.tsv --left-id source_id --right-id candidate_id --min-recall 0.60
   ```

3. **Create GitHub Release (Optional):**
   Once verified on PyPI, create a release on GitHub linked to the `v0.1.0` tag:
   ```bash
   gh release create v0.1.0 --title "CandGuard v0.1.0" --notes "First stable release of CandGuard: quality gates for entity-resolution candidate generation." dist/*
   ```
