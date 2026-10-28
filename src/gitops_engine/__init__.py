"""gitops-engine: reconcile declarative git definitions against a live system.

A consumer declares its records once (`RecordSchema`, `IdentitySpec`) and supplies a
`Destination`; the engine plans by identity and per-field policy, checks for drift,
applies under check-and-set with a snapshot, and restores. Submodules hold the rest:
datasets, manifests, writers, merge, found stubs, the state mirror, ignore and retire.
"""

from .check import CheckResult, DatasetHooks, identity_offline_errors, manifest_errors, run_check
from .deploy import ApplyResult, DeployPlan, apply_deploy, plan_deploy
from .destination import Destination, DestinationError
from .drift import DriftReport, detect_drift
from .exitcodes import EXIT_DRIFT, EXIT_OK, EXIT_OPERROR
from .identity import Identity, IdentitySpec, IdentityTable, ItemKeys, MatchKey
from .schema import (
    Always,
    FieldSpec,
    KeepIfUnset,
    Nouns,
    Policy,
    RecordSchema,
    SyncMark,
    Union,
)
from .snapshot import RestoreResult, restore

__version__ = "0.2.0"

__all__ = [
    "EXIT_DRIFT",
    "EXIT_OK",
    "EXIT_OPERROR",
    "Always",
    "ApplyResult",
    "CheckResult",
    "DatasetHooks",
    "DeployPlan",
    "Destination",
    "DestinationError",
    "DriftReport",
    "FieldSpec",
    "Identity",
    "IdentitySpec",
    "IdentityTable",
    "ItemKeys",
    "KeepIfUnset",
    "MatchKey",
    "Nouns",
    "Policy",
    "RecordSchema",
    "RestoreResult",
    "SyncMark",
    "Union",
    "apply_deploy",
    "detect_drift",
    "identity_offline_errors",
    "manifest_errors",
    "plan_deploy",
    "restore",
    "run_check",
]
