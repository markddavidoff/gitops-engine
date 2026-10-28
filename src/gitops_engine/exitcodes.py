"""Exit codes a CI gate keys off: 0 in sync, 2 an operational error, 3 drift or a
failed check (`check.run_check`)."""

from __future__ import annotations

EXIT_OK = 0
EXIT_OPERROR = 2
EXIT_DRIFT = 3
