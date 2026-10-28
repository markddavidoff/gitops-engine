# CLI contract

The reusable workflow in [`.github/workflows/sync.yml`](../.github/workflows/sync.yml)
runs a consumer's command-line tool and knows nothing else about it. It calls the tool
only through the commands below. A tool that implements them can use the workflow
unchanged.

The workflow installs the tool from the `tool` input (a uv requirement) and runs the
executable named by the `command` input. It passes `--config <config>` before every
subcommand, where `<config>` is the `config` input.

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
| `changed --since SHA` | Print `changed` or `unchanged` and exit 0. `changed` when a deploy input differs between `SHA` and HEAD, and when unsure. Use `gitops_engine.changed.changed_since`. |
| `check` | Compare the desired files, the state mirror and the live system. Exit 0 in sync, `EXIT_DRIFT` on drift or an aborting plan. Never writes. |
| `deploy --apply --prod --no-input` | Apply the plan under check-and-set after writing a snapshot. |
| `restore --from SNAPSHOT [--rows ID ...] --prod --no-input` | Write a snapshot back. `SNAPSHOT` is a path relative to the repository root. `--rows` takes one or more ids. |
| `sync-inbound --no-input` | Refresh the state mirror from the live system. |
| `refresh --no-input` | Refresh every file derived from the live system, in the tool's own order: the state mirror, then the datasets built from it, then the generated files. The first non-zero exit stops the run and is the exit code. Never writes to the live system. |
| `tool-source` | Print where the installed tool came from, as `git <commit> <url>` or `dir - <path>`, and exit `EXIT_OPERROR` when that cannot be recovered. The workflow's e2e mode uses it to fetch the tests at the installed version. |

`--no-input` means the command never prompts. `--prod` marks a write to the configured
data directory; the tool decides what it guards.

## Config keys

`config-get` answers these keys:

| Key | Value |
|---|---|
| `data_dir` | The directory the tool reads and writes, as a path. The workflow commits only under it. |
| `deploy_environment` | The GitHub environment that holds the approval gate and runs `deploy`. |
| `readonly_environment` | The GitHub environment that runs the scheduled `refresh`, with no approval. |
| `state_dir` | The state mirror directory. The state PR commits it. |
| `snapshot_dir` | The snapshot directory. The workflow uploads it as an artifact and the state PR commits it. |
| `e2e_tests` | The path of the live tests, relative to the root of the tool's source. The e2e mode runs `pytest <e2e_tests> --run-live -q` there. |

`data_dir`, `state_dir` and `snapshot_dir` must lie inside the repository.

## Token

The tool reads its destination token from one environment variable and the caller names
it in the `token-env` input. The workflow holds the token under a neutral name and
exports it under that name for each call, so the tool sees only its own variable.
`deploy`, `restore`, `check` and `sync-inbound` get the write token. `refresh` gets the
read-only token.
