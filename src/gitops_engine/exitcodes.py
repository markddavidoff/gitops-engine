"""Exit codes and the combined check a CI gate keys off."""

from __future__ import annotations

from .adapter import Adapter
from .drift import DriftReport, detect_drift
from .plan import Plan, make_plan

EXIT_OK = 0
EXIT_OPERROR = 2
EXIT_DRIFT = 3


def run_check[D, L, S](
    desired: list[D],
    state: list[S],
    live: list[L],
    adapter: Adapter[D, L],
) -> tuple[int, DriftReport[S, L], Plan[D, L]]:
    """Report drift and a plan together. EXIT_DRIFT when the state no longer matches
    live OR the plan would abort; EXIT_OK when everything reconciles cleanly."""
    drift = detect_drift(state, live, adapter)
    plan = make_plan(desired, live, state, adapter)
    code = EXIT_DRIFT if (drift.any() or plan.aborts) else EXIT_OK
    return code, drift, plan
