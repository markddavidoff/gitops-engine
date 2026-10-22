"""gitops-engine: reconcile declarative git definitions against a live system."""

from .adapter import Adapter, SyncMark
from .apply import apply
from .drift import DriftReport, detect_drift
from .exitcodes import EXIT_DRIFT, EXIT_OK, EXIT_OPERROR, run_check
from .plan import Plan, make_plan

__version__ = "0.1.0"

__all__ = [
    "EXIT_DRIFT",
    "EXIT_OK",
    "EXIT_OPERROR",
    "Adapter",
    "DriftReport",
    "Plan",
    "SyncMark",
    "apply",
    "detect_drift",
    "make_plan",
    "run_check",
]
