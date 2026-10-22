# Roadmap

## What this is for, and where it is going

`gitops-engine` reconciles a dataset kept in git with an external system through
plan, check, and apply (see `README.md`). The goal is a generic tool for "a dataset
in git, compared with an external system, reconciled through plan, check, apply and
pull requests": every change to either side becomes a reviewable pull request,
whether a person proposes it in git or the external system changed on its own.

First consumer: `customer-registry-sync`, which syncs a registry sourced from Notion
to Linear Customers. Reading it is not required to work on this repo.

## Done

- **Core extracted.** Plan, drift detection by content hash, check-and-set apply,
  and `run_check` with its exit codes, driven entirely through the `Adapter`
  protocol. Tests cover every path with an in-memory toy adapter.
- **Adopted by the first consumer (2026-10-21).** `customer-registry-sync` now runs
  its plan and check on this engine through its own adapter, with no behavior
  change.

## Planned engine features

### Field ownership and value convergence

**Proposal, not yet agreed. Only the ownership rules are drafted.**

- [ ] **(a) Field ownership policies.** Per field, the adapter declares one of three
  policies:
  - *always-desired*: the desired value is written, whatever the external system
    holds.
  - *union*: for multi-valued fields. The written value is the desired values plus
    any values that exist only externally; those external-only extras are kept and
    reported as "untracked".
  - *keep-if-unset*: when the desired row sets the field, the desired value wins.
    When it leaves the field unset, the external value is kept and reported as
    "untracked".
- [ ] **(b) Untracked values, absorb, and retire.**
  - `run_check` gains a third result next to drift and plan: *untracked values*,
    each external value the dataset in git does not carry. It exits 3 until they
    are absorbed.
  - An *absorb* step records those external-only values into a curated input file
    in git, so the dataset matches the external system before any apply and nobody
    hand-edits generated files.
  - A *retire* list names, per record, the values to remove, each with its exact
    current value. It is the only way a value would ever be removed, and an entry
    whose value is already gone is flagged stale. Removing a value takes two
    reviewable steps: the retire entry in a pull request, then the apply.
  - Removal is a new, opt-in capability that does not exist today. Current
    behavior, as `README.md` states, is that the engine never deletes.

### Pull-request automation

- [ ] **(c) Post-apply state refresh.** After an apply (for example on merge, behind
  an approval gate), observe the external system again, re-record state, and open a
  pull request with the refreshed state.
- [ ] **(d) Scheduled drift check.** On a schedule, observe the external system and
  open a pull request with the new state when it changed out of band.
- [ ] **(e) CI credential for opening pull requests.** (c) and (d) both need one;
  decide this first. Options:
  - a fine-grained personal access token scoped to the one repository;
  - a GitHub App with short-lived installation tokens;
  - no pull request from CI: upload the refreshed state as a workflow artifact and
    open the pull request by hand.

## Library work

- [ ] **Importable runnable example.** `examples/toy.py` runs from a clone; make the
  toy adapter importable from an installed package too.
- [ ] **Typing.** Ship `py.typed`, and give the `Adapter` protocol a state type
  parameter (today only the functions are generic over the state row type).
- [ ] **Lifecycle docs.** A guide to writing an adapter and running the
  observe, record, check, apply, re-record loop in CI.
- [ ] **Packaging and publishing.** Tagged releases and a changelog; decide whether
  to publish to PyPI or stay a git URL dependency.
