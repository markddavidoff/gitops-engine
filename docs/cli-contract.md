# CLI contract

The reusable workflow in [`.github/workflows/sync.yml`](../.github/workflows/sync.yml)
runs a consumer's command-line tool and knows nothing else about it. It calls the tool
only through the commands below. A tool that implements them can use the workflow
unchanged.

The workflow installs the tool from the caller's locked uv project (the `project_dir`
input, with a committed `uv.lock`): `uv sync --locked`, then `uv run --frozen`. The lock
pins the tool and every transitive dependency, the same in every job, so a job that holds
a token runs only locked code. The lock must use https git URLs or an index; the jobs
hold no deploy key. The workflow runs the executable named by the `command` input and
passes `--config <config>` before every subcommand, where `<config>` is the `config` input.

## Exit codes

The `EXIT_*` constants in `gitops_engine.exitcodes`:

| Constant | Value | Meaning |
|---|---|---|
| `EXIT_OK` | 0 | The command succeeded; in sync, with no aborts in the plan. |
| `EXIT_OPERROR` | 2 | An operational error: bad input, a missing key, a refused write. |
| `EXIT_DRIFT` | 3 | `check` found drift, or the plan would abort. |

The workflow treats any non-zero exit as a failure, except `changed`, which always exits 0.

## Commands

| Command | What the workflow expects |
|---|---|
| `config-get KEY` | Print one value on stdout, newline-terminated. Exit `EXIT_OPERROR` with the missing key named on stderr when the tool cannot answer. |
| `changed --since SHA` | Print `changed` or `unchanged` and exit 0. `changed` when a deploy input differs between `SHA` and HEAD, and when unsure. Use `gitops_engine.changed.changed_since`. The files under `state_dir` and `snapshot_dir` must not be deploy inputs: the state PR commits them, and counting them would make its merge start another deploy. |
| `check` | Compare the desired files, the state mirror and the live system. Exit 0 in sync, `EXIT_DRIFT` on drift or an aborting plan. Never writes. |
| `deploy --apply --prod --no-input` | Apply the plan under check-and-set after writing a snapshot. |
| `restore --from SNAPSHOT [--rows ID ...] --prod --no-input` | Write a snapshot back. `SNAPSHOT` is a path relative to the repository root. `--rows` takes one or more ids. |
| `sync-inbound --no-input` | Refresh the state mirror from the live system. |
| `refresh --no-input` | Refresh every file derived from the live system, in the tool's own order: the state mirror, then the datasets built from it, then the generated files. The first non-zero exit stops the run and is the exit code. Never writes to the live system. Every file it writes goes under `data_dir`, because the refresh PR commits all of `data_dir`; it must write nothing there that is not meant for review. |
| `tool-source` | Print where the installed tool came from, as `git <commit> <url>` or `dir - <path>`, and exit `EXIT_OPERROR` when that cannot be recovered. The workflow's e2e mode uses it to fetch the tests at the installed version. |

`--no-input` means the command never prompts. `--prod` marks a write to the configured
data directory; the tool decides what it guards.

## Config keys

`config-get` answers these keys:

| Key | Value |
|---|---|
| `data_dir` | The directory the tool reads and writes, as a path. The refresh PR commits all of it. |
| `deploy_environment` | The GitHub environment that holds the approval gate and runs `deploy`. |
| `readonly_environment` | The GitHub environment that runs the scheduled `refresh`, with no approval. |
| `state_dir` | The state mirror directory. The state PR commits it. It is not a deploy input. |
| `snapshot_dir` | The snapshot directory. The workflow uploads it as an artifact and the state PR commits it. It is not a deploy input. |
| `e2e_tests` | The path of the live tests, relative to the root of the tool's source: no `..` segment, not absolute, not starting with `-`. The e2e mode runs `pytest <e2e_tests> --run-live -q` there. |

`data_dir`, `state_dir` and `snapshot_dir` must lie inside the repository. The workflow
refuses any `config-get` value that contains a newline before it writes the value to a
step output.

## Token

The tool reads its destination token from one environment variable and the caller names
it in the `token-env` input. The workflow holds the token under a neutral name and
exports it under that name for each call, so the tool sees only its own variable.
Each token is a secret of the same name that the environment holds: `DESTINATION_TOKEN`
in the deploy environment, `DESTINATION_READONLY_TOKEN` in the read-only environment. An
environment secret takes precedence over a value the caller passes, so the write token
is released only after a required reviewer approves the deploy.

| Step | Commands | Token |
|---|---|---|
| deploy job | `check`, `deploy`, `restore`, `sync-inbound` | write token |
| deploy job, e2e mode | `pytest <e2e_tests> --run-live` | write token |
| refresh job | `refresh` | read-only token |
| every other step | `changed`, `config-get`, `tool-source` and the PR, upload and checkout steps | none |

`refresh` is the one command that holds the read-only token for more than the live read:
it runs `sync-inbound`, and the workflow cannot split it. A tool should give the token
only to the step that reads the live system and keep it from the offline steps.
