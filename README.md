# gitops-engine

Reconcile records kept in git with a live external system. The engine plans by
identity and per-field policy, detects drift, applies writes under a check-and-set with
a snapshot, verifies each write, and restores from a snapshot. It knows nothing about
any specific system. A consumer supplies a record schema and a destination.

## Why, and who it is for

Use it when a list of records (accounts, DNS records, feature flags, rows in a SaaS
tool) should be reviewed in pull requests and then pushed to a system that people also
edit by hand. The engine answers four questions: what would a deploy change, has the
destination moved since we last looked, can we write without clobbering someone else's
edit, and did the write land as intended. The consumer supplies the system-specific
parts; the engine supplies the decision rules, the file formats and the exit codes a CI
gate keys off.

A second sync (any source, any destination) needs only a record schema with its field
policies, a destination client, and a command-line tool that implements the
[CLI contract](docs/cli-contract.md).

## Install

Requires Python 3.12 or newer. The package is not on PyPI. Depend on it by https git
URL, pinned to a tag:

```toml
# pyproject.toml
dependencies = ["gitops-engine @ git+https://github.com/markddavidoff/gitops-engine@v0.2.0"]
```

v0.2.0 replaces the v0.1 `Adapter` / `make_plan` / `apply` / `run_check` API. There is
no compatibility layer.

## Run the example

From a clone:

    uv sync
    uv run python examples/toy.py

It walks the lifecycle against an in-memory destination (`examples/toy_adapter.py`):
mirror, check, apply, check again, then an out-of-band edit. It prints the exit code
and the plan at each step.

## The four row types

- **desired** (`D`): what git says should exist. The source of truth for the fields
  you manage.
- **live** (`L`): what the destination reports now. Carries an id and a `version`
  token (an updatedAt, etag or revision).
- **state**: a recorded reconciliation point. A desired-shaped row plus a `SyncMark`
  (`live_id`, `version`, `content_hash`) for the exact live row it was last reconciled
  against. The state directory is the *mirror* of the destination. A state row with no
  mark is untracked and proves nothing.
- **dataset record** (`R`): a source record in a named dataset directory. Datasets are
  merged by identity into the desired rows.

The engine compares **content hashes** of the planned fields, never the raw `version`.
A destination often bumps `version` on edits to fields you do not manage. That is a
benign metadata bump, not drift.

## What a consumer supplies

### `RecordSchema` (`gitops_engine.schema`)

One declaration of how the engine reads a record kind:

- `nouns`: a `Nouns(record, records, destination, source, version)` the engine prints
  in its messages, so a consumer's output carries its own words.
- `fields`: a tuple of `FieldSpec(name, policy, required=False)`, in plan and verify
  order.
- `id_field`: the `Always(unordered=True)` field that carries canonical ids.
- `snapshot_id_key`, `snapshot_version_key`: the keys a snapshot row stores.
- Accessors: `desired_value`, `live_value`, `label`, `live_label`, `live_id`,
  `live_version`, `desired_keys`, `live_keys` (what `resolve` matches on), `mark`,
  `destination_owned`, `with_values`, `mirror_row`, `live_hash` (equal to the hash in
  `mark(mirror_row(lv))`), `state_to_live`, `snapshot_row` and `snapshot_values`.

The engine holds no record type of its own.

### Field policies

Each `FieldSpec` carries one policy:

| Policy | Applies to | Rule |
|---|---|---|
| `Always(unordered=False)` | scalars and lists | Desired replaces live. With `unordered`, values compare sorted and every difference is a plain change. |
| `Union(key, exclusive, item)` | lists | The written value is the desired values plus values that exist only on the destination. Destination-only values are kept and reported as untracked. A destination-only value is dropped only when a retire entry names it. `key` normalises values before comparing (lower case by default), so a spelling that differs only by case plans no change and keeps the live spelling. With `exclusive`, a value another live record already holds is withheld, not written. |
| `KeepIfUnset` | scalars | When the desired row sets the field, desired wins. When it leaves the field unset, the live value is kept and reported as untracked. |

`FieldSpec.required` has two effects:

- On a destination-owned row whose live value is empty, the planner falls back to the
  desired value.
- Post-write verification expects a field with no planned change to equal its value
  from before the write if the field is `required`, and to equal the plan's target
  otherwise. So `required` also sets the verification expectation for unplanned
  fields.

A destination-owned row (`destination_owned`) takes every value from live and writes
only its canonical id.

### `IdentitySpec` (`gitops_engine.identity`)

Declares the identity table's layout and matching: the source names, the ordered
`MatchKey(field, label, norm, skip_empty)` list, and the key order inside each entry of
the file. `resolve` tries keys in a fixed order: canonical id, then each source id in
declared order, then each match key in declared order. The first hit wins, and a
conflict message names both hits.

### `Destination` (`gitops_engine.destination`)

Where planned writes land. Values travel as dicts keyed by the schema's field names.

| Method | Purpose |
|---|---|
| `list_live() -> list[L]` | every live row |
| `get(live_id) -> L \| None` | one row as it is now, or None |
| `preflight(writes, *, restoring=False)` | raise `ValueError` before any write when a write would fail to resolve |
| `create(values) -> L` | create a row; return the live row written |
| `update(live_id, values, *, retire) -> L` | write exactly `values`; `retire` allows an empty value to clear a field |

A refused or failed write raises `DestinationError`.

## Data files

The engine owns the formats of the files that sit under a consumer's data directory.
Every file name is a parameter.

| Mechanism | Module | What it does |
|---|---|---|
| Datasets | `datasets`, `writers` | One directory per dataset, a `datasets.yaml` priority order, deterministic writers. |
| Identity table | `identity`, `identity_store` | The table, a comment-preserving appender, ordered-key `resolve`, conflict and integrity checks. |
| Merge | `merge` | One record per identity. The earliest dataset in priority order wins whole; extra keys gather onto the winner. |
| Destination-only stubs | `found` | Resolve each managed live row, mint identities for unmatched ones, emit stub records. |
| Ignore list | `ignore` | Match live rows by id or name prefix; a built-in test prefix applies to unscoped runs. |
| Retire file | `retire` | Per record, the values to remove, each with its exact current value. The only way a value is ever removed; an entry whose value is already gone is reported stale. |
| Manifests | `manifest` | A hash manifest beside generated output. `check` fails on hand edits and stale output. |
| State mirror | `mirror` | Reconcile the live rows into mirror rows with their marks. |

## Plan, check, apply, restore

### Plan (`plan_deploy`)

Pairs each desired row with live rows by identity, then plans each field under its
policy. Several live rows can share one identity: one primary is written and the rest
are reported as known duplicates or as an error. A `DeployPlan` holds `creates`,
`updates` (target row and live row), `aborts`, `orphans`, `unchanged`, `known_duplicates`,
`withheld`, `notes`, `errors` and `unacked` (destructive changes not yet acknowledged
by a retire entry). Orphans are reported and never deleted.

### Check (`run_check`)

Drift (state against live, by content hash), the plan, untracked values, stale retire
entries and the live checks, as one `CheckResult`. Drift classes are **changed**,
**added** (a live row no tracked state row points at) and **deleted** (a tracked row
whose live row is gone). With dataset hooks it also fails orphans and live identity
conflicts, and reports unused identities and records with no identity.

Exit codes (`gitops_engine.exitcodes`):

- `EXIT_OK = 0`: in sync, and the plan has no aborts.
- `EXIT_DRIFT = 3`: drift was found or the plan would abort.
- `EXIT_OPERROR = 2`: an operational error. The library never returns it; a tool
  returns it when a step fails (authentication, network, bad input), so CI can tell
  "could not run" from "found drift".

### Apply (`apply_deploy`)

Order: refuse a plan with errors or unacknowledged destructive changes, run
`preflight`, snapshot every live row about to be updated, create rows, then for each
update re-read the row (check-and-set), match it against the ignore list, abort on
write-time drift, apply the never-null guard, call `update` with only the planned
fields, record the written version in the snapshot and verify the row returned.

- **Never-null guard.** A write that holds `None`, `""` or `[]` is refused before the
  destination is called, unless the change is a retire.
- **Stop at the first failure.** A destination error, a guard refusal or a
  verification mismatch stops the apply. Later updates are recorded in `plan.aborts` as
  not attempted. The engine never deletes.
- **Not transactional.** Rows already written stay written. Creates are not
  idempotent; pairing by identity is what keeps a rerun from duplicating a row.
- **Check-and-set is get-then-update.** A write can land between the re-read and the
  `update`, so the check is best effort unless the destination's own `update` enforces
  the version.

### Restore (`restore`)

Writes a snapshot back under the same check-and-set, skipping rows edited since. `rows`
limits it by canonical id. A row holding an empty value is sent as a retire. Snapshots
written by v0.1.0 load and restore with the same writes.

## The CLI contract

`.github/workflows/sync.yml` is a reusable workflow that runs a tool's command line and
knows nothing else about it. A tool implements `config-get`, `changed`, `check`,
`deploy`, `restore`, `sync-inbound`, `refresh` and `tool-source`, and answers the
config keys the workflow reads. See [`docs/cli-contract.md`](docs/cli-contract.md) for
the commands, config keys, exit codes and token model.

## The workflow and its secret model

The caller's repository holds a uv project (input `project_dir`, with a committed
`uv.lock`) that depends on the tool. Each job runs `uv sync --locked`, then calls the
tool through `uv run --frozen`, so a job that holds a token runs only locked code. The
lock must use https git URLs or an index; the jobs hold no deploy key.

Modes: on a push the workflow runs `changed --since` and ends when no deploy input
changed; otherwise it runs `deploy` behind an approval gate. A schedule runs `refresh`.
A dispatch runs `deploy`, `restore`, `e2e` or `refresh`. A deploy or refresh opens a
state or refresh pull request with a GitHub App token, so its checks run.

Secrets:

- The **deploy environment** holds the approval gate and the write token, as the secret
  `DESTINATION_TOKEN`. The **read-only environment** holds the read-only token, as
  `DESTINATION_READONLY_TOKEN`. An environment secret takes precedence over a value the
  caller passes, so the write token is released only after a required reviewer approves
  the deploy. The caller does not pass either token.
- The caller passes `APP_ID` and `APP_PRIVATE_KEY` for the pull-request App.
- The tool reads its token from one environment variable, named by the `token-env`
  input. The workflow holds the token under a neutral name and exports it under that
  name for each call.
- Tokens reach only the steps that call the destination (`check`, `deploy`, `restore`,
  `sync-inbound`, the live tests, and `refresh`), never job-level `env`, so checkout,
  upload and pull-request steps never see them.

## Development

    uv sync
    uv run pytest -q
    uv run ruff check . && uv run ruff format --check .

Tests drive the engine only through the toy schema and destination in `examples/`.

## License

MIT. See `LICENSE`.
