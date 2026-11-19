"""CandGuard: quality gates for entity-resolution candidate generation."""

__version__ = "0.1.0"

from .audit import audit_candidates
from .compare import compare_reports
from .models import AuditError

__all__ = [
    "AuditError",
    "__version__",
    "audit_candidates",
    "compare_reports",
]
