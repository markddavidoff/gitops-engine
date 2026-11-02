# Roadmap

## What this is for, and where it is going

`gitops-engine` reconciles records kept in git with an external system through plan,
check, and apply (see `README.md`). The goal is a generic tool for "a dataset in git,
compared with an external system, reconciled through plan, check, apply and pull
requests": every change to either side becomes a reviewable pull request, whether a
person proposes it in git or the external system changed on its own.

First consumer: `customer-registry-sync`, which syncs a registry sourced from Notion
to Linear Customers. Reading it is not required to work on this repo.

## Done

- **Core extracted (v0.1.0).** Plan, drift detection by content hash, check-and-set
  apply and `run_check`, driven through an `Adapter` protocol.
- **Adopted by the first consumer (2026-10-21).** It ran drift detection and the
  `check` exit codes on the engine and kept the rest locally.
- **v0.2.0: the seams the first port found, moved into the engine.** The v0.1 `Adapter`
  is replaced by a `RecordSchema` and a `Destination`; there is no compatibility layer.
  - **(a) Field ownership policies.** `Always`, `Union` and `KeepIfUnset`, declared per
    field in `FieldSpec`. A `Union` field can be `exclusive`. `FieldSpec.required`
    sets the fallback for a destination-owned row and the verification expectation for
    an unplanned field. A destination-owned row takes every value from live.
  - **(b) Untracked values and retire.** `run_check` reports each destination value the
    dataset does not carry and each stale retire entry. The retire file is the only way
    a value is removed, and it takes two reviewable steps: the entry in a pull request,
    then the apply. The never-null guard refuses an empty write unless it is a retire.
  - **(c) Post-apply state refresh, (d) scheduled drift check.** The reusable workflow
    runs `sync-inbound` after a deploy and `refresh` on a schedule, each opening a pull
    request.
  - **(e) CI credential.** The workflow opens those pull requests with a GitHub App
    token. The destination tokens stay environment secrets
    (`DESTINATION_TOKEN`, `DESTINATION_READONLY_TOKEN`), so the write token is released
    only after a required reviewer approves a deploy.
  - **Field-level changes, many-to-one pairing, snapshot and restore, post-write
    verification, never-null guard, manifests.** All in the engine, with tests that use
    only the toy schema.
  - **Data formats.** Dataset directories with a priority order, the identity table
    with ordered-key `resolve`, merge by identity, destination-only stubs, the ignore
    list, the retire file and the state mirror.
  - **Reusable workflow and CLI contract.** `.github/workflows/sync.yml` and
    `docs/cli-contract.md`. The caller supplies a locked uv project, so every job runs
    locked code.
- **v0.3.0: the registry owns every written field.** It replaces the planned "absorb"
  step; every new parameter defaults to the v0.2.0 behavior.
  - **Clearing policies.** `Always(clears=True)` and `Union(clears=True)` write an empty
    desired value and remove live values the desired list lacks, so retire entries no
    longer apply to those fields. A `required` field cannot clear.
  - **Overrides.** One file of pins, list additions and removals, pending edits, hidden
    and deleted rows, with a validator, an applier, a checker and a comment-preserving
    store. A row with a pending edit is held: the plan aborts it.
  - **Edit detection.** The previous mirror is compared with live, field by field.
    A cleared optional value or a removed list value is recorded as an accepted
    override; any other change becomes a pending entry, which a person resolves by
    pinning the value or deleting the entry. This replaces absorb.
  - **Deletion confirmation.** A mirrored row is recorded as deleted only when it is
    missing from the listing and `Destination.get` reports it not found. An error never
    confirms a deletion.
  - **Hidden rows.** Rows are excluded by canonical id or name prefix, in place of the
    ignore list, which stays for other consumers.

## Planned engine features

None open. The one planned feature, absorb, shipped in v0.3.0 (see Done).

## Library work

- [ ] **Importable runnable example.** `examples/toy.py` runs from a clone; make the
  toy schema importable from an installed package too.
- [ ] **Typing.** Ship `py.typed`.
- [ ] **Lifecycle docs.** A guide to writing a schema and a destination and to running
  the observe, record, check, apply, re-record loop in CI.
- [ ] **Packaging and publishing.** Tagged releases and a changelog; decide whether to
  publish to PyPI or stay a git URL dependency.
