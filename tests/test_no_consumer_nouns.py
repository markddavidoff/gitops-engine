"""The engine names no consumer: its source, README, docs and workflows stay generic."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOUNS = re.compile("customer|linear|notion", re.IGNORECASE)
SCANNED = ["src", "README.md", "docs", ".github"]
SKIP_DIRS = {"__pycache__", ".venv", ".git"}


def _files():
    for name in SCANNED:
        path = ROOT / name
        if path.is_file():
            yield path
        elif path.is_dir():
            for p in sorted(path.rglob("*")):
                if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts):
                    yield p


def test_no_consumer_nouns():
    offenders = []
    for path in _files():
        rel = str(path.relative_to(ROOT))
        if NOUNS.search(rel):  # a file name counts, such as one the consumer chose
            offenders.append(rel)
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if NOUNS.search(line):
                offenders.append(f"{rel}:{lineno}")
    assert offenders == []
