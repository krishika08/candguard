# Contributing to CandGuard

Thank you for your interest in contributing to CandGuard!

CandGuard is an experimental, local-first Python CLI for auditing entity-resolution candidate-pair output and enforcing quality thresholds in CI pipelines.

## Core Architectural Constraints

Before proposing changes, please keep in mind CandGuard's fundamental constraints:
1. **Zero Runtime Dependencies:** The production package must rely solely on the Python Standard Library (3.10+) and built-in SQLite. Do not add runtime dependencies without prior discussion and strong benchmark justification.
2. **Local-First & Safe:** CandGuard never transmits data over the network or writes outside user-designated paths. Temporary databases and files must be cleaned up on both success and error.
3. **Deterministic & Cross-Platform:** Commands and exit codes must behave consistently on Windows, macOS, and Linux.
4. **Contract Rigor:**
   - Exit code `0`: All quality gates passed.
   - Exit code `1`: One or more quality gates failed.
   - Exit code `2`: Invalid input, configuration, or operational error.

## Development Setup

Requires Python 3.10 or newer.

```bash
# Clone the repository
git clone https://github.com/krishika08/candguard.git
cd candguard

# Create and activate a virtual environment
python -m venv .venv
# On Linux/macOS:
source .venv/bin/activate
# On Windows PowerShell:
.venv\Scripts\Activate.ps1

# Install in editable mode with development dependencies
pip install -e ".[dev]"
```

## Running Tests & Verification

Run the test suite and coverage report:

```bash
pytest --cov=candguard
```

Run CLI smoke tests:

```bash
candguard audit \
  --candidates examples/candidates.tsv \
  --truth examples/truth.tsv \
  --left-id source_id \
  --right-id candidate_id \
  --min-recall 0.60
```

Verify distribution package builds:

```bash
pip install build
python -m build
```

## Pull Request Guidelines

1. **Add Tests:** Every bug fix or feature addition must include automated regression tests covering both happy paths and edge cases (e.g. empty files, duplicate rows, whitespace, delimiters).
2. **Preserve Contracts:** Do not alter CLI flags, exit code meanings, or JSON report schemas without prior consensus.
3. **Keep Fixtures Small:** Keep test fixtures small and synthetic. Do not commit large generated benchmark datasets to git.
4. **Clean Builds:** Ensure `python -m build` succeeds without packaging warnings or unneeded files.
