# gitops-engine

Reconcile a dataset kept in git against a live external system. The engine computes
a plan, detects drift, and applies writes with a write-time version check. It knows
nothing about any specific system; you supply an `Adapter`.

## Why, and who it is for

Use it when a list of records (accounts, DNS records, feature flags, rows in a SaaS
tool) should be reviewed in pull requests and then pushed to a system that people
also edit by hand. The engine answers three questions: what would a deploy change,
has the external system moved since we last looked, and can we write without
clobbering someone else's edit. You write the adapter for your system; the engine
supplies the decision rules and the exit codes a CI gate keys off.

## Install

Requires Python 3.12 or newer. The package is not on PyPI; depend on it by git URL,
pinned to a tag:

```toml
# pyproject.toml
dependencies = ["gitops-engine @ git+https://github.com/markddavidoff/gitops-engine@v0.1.0"]
```


## Run the example

From a clone:

    uv sync
    uv run python examples/toy.py

It walks the full lifecycle against an in-memory adapter (`examples/toy_adapter.py`)
and prints the exit code and plan at each step:

    first check: 0 | creates ['cog'] | updates ['gear']
    after apply: 0 | unchanged ['gear', 'cog']
    after out-of-band edit: 3 | changed ['gear'] | aborts [('gear', 'drift: live version v9 != state version v-upd (content changed)')]

## The three record shapes

- **desired**: what git says should exist. The source of truth for the fields you
  manage.
- **live**: what the external system currently reports. Carries an identity and a
  `version` token (an updatedAt, etag, or revision).
- **state**: a recorded reconciliation point: a record plus a `SyncMark`
  (`live_id`, `version`, `content_hash`) capturing the exact live row it was last
  reconciled against. A state row with no mark is *untracked* and proves nothing.

The engine compares **content hashes** of the deployable fields, never the raw
`version`. An external system often bumps `version` on edits to fields you do not
manage; that is a benign metadata bump, not drift.

## Lifecycle

1. **Observe** the external system: read its rows (your code, usually through your
   adapter's client).
2. **Record state**: store one state row per live row, each carrying a `SyncMark`
   taken from that live row. Keep the state in git next to the desired rows.
3. **Check** with `run_check`: drift between state and live, plus the plan.
4. **Apply** the plan with `apply`.
5. **Re-record** the marks from the live rows `apply` returns, so the next check
   starts from what was just written.

On the first run nothing has been recorded, so there are no marks and no row can
abort at plan time. Until state is recorded, every live row counts as **added**
drift and `run_check` exits 3; recording state (step 2) clears that.

## Plan (`make_plan`)

    make_plan(desired, live, state, adapter) -> Plan

For each desired row the engine asks the adapter to `match` it to a live row:

- no match: **create**;
- matched, and a mark exists whose `version` differs from live **and** whose
  recorded hash differs from the live hash: **abort** (the row changed under you
  since the mark was taken);
- matched and the desired hash equals the live hash: **unchanged**;
- otherwise: **update**.

A live row that no desired row matched is an **orphan**: reported, never deleted.
Which live row a desired row matches, and what happens when several could, is the
adapter's choice in `match`; the engine does not pick. (The toy adapter takes the
first one with the same key.)

An orphan is a plan-side fact: a live row with no *desired* row. **Added** drift is
a state-side fact: a live row with no *state* row. One live row can be both, either,
or neither.

## Drift (`detect_drift`)

    detect_drift(state, live, adapter) -> DriftReport

Drift is the recorded state no longer matching live, judged by content hash. Three
classes:

- **changed**: a tracked state row whose live row's hash no longer matches the mark;
- **added**: a live row no tracked state row points at;
- **deleted**: a tracked state row whose live row is gone.

Untracked state rows (no mark) are skipped.

## Apply (`apply`)

    apply(plan, adapter) -> list of the live rows written

`apply` runs `preflight` first; if it raises, nothing is written. It then performs
creates, then updates. It never deletes. What it does and does not guarantee:

- **Rows aborted at plan time are skipped; every other row is written.** One abort
  does not stop the apply, so inspect `plan.aborts` before calling `apply`.
- **`apply` mutates the plan.** Write-time aborts are appended to `plan.aborts`;
  inspect it again afterwards.
- **The write-time check is get-then-update.** Before each update the engine calls
  `get` and skips the row (appending to `plan.aborts`) if it is gone or its
  `version` moved since plan time. A write can still land between the `get` and the
  `update`, so the check is best effort unless your adapter's `update` itself
  enforces the version (for example with an `If-Match` header).
- **Creates are unguarded and not idempotent.** A create that runs twice, or races
  another writer, can produce a duplicate. Make `match` find existing rows reliably.
- **Not transactional.** A write that raises propagates, and rows already written
  stay written.

## Check and exit codes (`run_check`)

    run_check(desired, state, live, adapter) -> (code, DriftReport, Plan)

Note the argument order: `make_plan` takes `(desired, live, state, adapter)`, while
`run_check` takes `(desired, state, live, adapter)`. Both are kept as they are for
compatibility. The parameter names match, so passing them by keyword avoids mix-ups.

- `EXIT_OK = 0`: in sync, and the plan has no aborts.
- `EXIT_DRIFT = 3`: drift was found or the plan would abort.
- `EXIT_OPERROR = 2`: an operational error. The library never returns it. Callers
  are expected to return it when an operational step fails (authentication,
  network), so CI can tell "the check could not run" from "the check found drift".

## The Adapter contract

An adapter supplies reads, identity, and writes for one system:

| Method | Purpose |
|---|---|
| `match(d, live) -> L \| None` | the live row a desired row corresponds to |
| `desired_hash(d) -> str` | hash of a desired row's deployable fields |
| `live_hash(lv) -> str` | hash of a live row's deployable fields |
| `live_id(lv) -> str` | stable identity of a live row |
| `live_version(lv) -> str` | optimistic-concurrency token of a live row |
| `mark_for(d, lv, state) -> SyncMark \| None` | the mark recorded for this pair |
| `state_mark(s) -> SyncMark \| None` | the mark on a state row, or None |
| `preflight(plan) -> None` | validate before any write; raise to abort |
| `get(live_id) -> L \| None` | re-read a live row at write time |
| `create(d) -> L` | create a row; return the live row written |
| `update(live_id, d) -> L` | update a row; return the live row written |

The engine locates a state row only through `mark_for` and `state_mark`, so how
state rows are keyed is the adapter's business. `examples/toy_adapter.py` is a
complete adapter to copy from.

## Development

    uv sync
    uv run pytest -q
    uv run ruff check . && uv run ruff format --check .

## License

MIT. See `LICENSE`.
