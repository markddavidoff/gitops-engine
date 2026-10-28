"""Checks of the reusable sync workflow (`.github/workflows/sync.yml`).

No network and no Actions run: these parse the YAML and pin the gates the workflow must
keep (modes, approval environment, snapshot artifact, App-token PRs, separate concurrency
groups, explicit secrets) and that it calls the tool only through the CLI contract in
`docs/cli-contract.md`. Two scripts (the mode decision and the install step) run locally
under bash with stub commands.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REUSABLE = ROOT / ".github" / "workflows" / "sync.yml"
WRAPPER = "gitops-tool"  # the install step's wrapper around the tool's command
MODES = ("deploy", "restore", "e2e", "refresh")
INPUTS = {"mode", "snapshot", "rows", "tool", "command", "config", "token-env", "base_branch"}
SECRETS = {"destination-token", "destination-readonly-token", "app-id", "app-private-key"}
CONFIG_KEYS = {
    "data_dir",
    "deploy_environment",
    "readonly_environment",
    "state_dir",
    "snapshot_dir",
    "e2e_tests",
}
# The CLI contract: each subcommand the workflow may call, with the flags it may pass.
CONTRACT: dict[str, set[str]] = {
    "config-get": set(),
    "changed": {"--since"},
    "check": set(),
    "deploy": {"--apply", "--prod", "--no-input"},
    "restore": {"--from", "--rows", "--prod", "--no-input"},
    "sync-inbound": {"--no-input"},
    "refresh": {"--no-input"},
    "tool-source": set(),
}


def _load(path: Path = REUSABLE) -> dict:
    return yaml.safe_load(path.read_text())


def _triggers(wf: dict) -> dict:
    # PyYAML (YAML 1.1) parses the bare key `on` as the boolean True.
    return wf.get("on", wf.get(True))


def _runs(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def _step_using(job: dict, action: str) -> list[dict]:
    return [s for s in job["steps"] if s.get("uses", "").startswith(action + "@")]


def _step_named(job: dict, prefix: str) -> dict:
    return next(s for s in job["steps"] if s.get("name", "").startswith(prefix))


def test_reusable_workflow_shape():
    wf = _load()
    triggers = _triggers(wf)
    assert set(triggers) == {"workflow_call"}, "the reusable workflow has no triggers of its own"
    call = triggers["workflow_call"]
    inputs = call["inputs"]
    assert set(inputs) == INPUTS
    for mode in MODES:
        assert mode in inputs["mode"]["description"]
    assert inputs["config"]["default"] == "registry-sync.yaml"
    for name in ("tool", "command", "token-env"):
        assert inputs[name]["required"] is True and "default" not in inputs[name]
    assert set(call["secrets"]) == SECRETS
    assert all(spec["required"] is False for spec in call["secrets"].values())

    jobs = wf["jobs"]
    assert set(jobs) == {"plan", "deploy", "refresh"}
    plan = jobs["plan"]
    plan_runs = _runs(plan)
    # The first job decides whether to run, and reads the environments from config.
    assert "changed --since" in plan_runs
    assert "config-get deploy_environment" in plan_runs
    assert "config-get readonly_environment" in plan_runs
    assert set(plan["outputs"]) == {"run", "mode", "deploy_environment", "readonly_environment"}
    assert _step_using(plan, "actions/checkout")[0]["with"]["fetch-depth"] == 0

    deploy, refresh = jobs["deploy"], jobs["refresh"]
    for job in (deploy, refresh):
        assert job["needs"] == "plan"
        assert "needs.plan.outputs.run == 'true'" in job["if"]
        assert job["permissions"] == {"contents": "read"}
    assert "needs.plan.outputs.mode != 'refresh'" in deploy["if"]
    assert "needs.plan.outputs.mode == 'refresh'" in refresh["if"]

    # Deploy jobs are bound to the configured environments (the approval gate lives there).
    assert deploy["environment"] == "${{ needs.plan.outputs.deploy_environment }}"
    assert refresh["environment"] == "${{ needs.plan.outputs.readonly_environment }}"

    deploy_runs = _runs(deploy)
    assert f"{WRAPPER} check" in deploy_runs
    assert "deploy --apply --prod --no-input" in deploy_runs
    assert "restore --from" in deploy_runs
    assert "--run-live" in deploy_runs
    assert "config-get e2e_tests" in deploy_runs

    # The snapshot upload reads the directory the tool declares, not a fixed name.
    upload = _step_using(deploy, "actions/upload-artifact")
    assert len(upload) == 1
    assert upload[0]["with"]["path"] == "${{ steps.paths.outputs.snapshot_dir }}/"
    assert upload[0]["if"].startswith("always()")

    # The refresh job is one contract command, run with the read-only token; no `check`.
    refresh_runs = _runs(refresh)
    assert f"{WRAPPER} refresh --no-input" in refresh_runs
    assert not re.search(r"\bcheck\b", refresh_runs.replace("checkout", ""))

    for job in (deploy, refresh):
        token = _step_using(job, "actions/create-github-app-token")
        assert len(token) == 1
        assert token[0]["with"]["app-id"] == "${{ secrets.app-id }}"
        assert token[0]["with"]["private-key"] == "${{ secrets.app-private-key }}"
        pr_step = next(s for s in job["steps"] if "gh pr create" in s.get("run", ""))
        assert pr_step["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"


def test_refresh_uses_the_read_only_token():
    refresh = _load()["jobs"]["refresh"]
    tokens = [
        s["env"]["GITOPS_TOKEN"] for s in refresh["steps"] if "GITOPS_TOKEN" in s.get("env", {})
    ]
    assert tokens == ["${{ secrets.destination-readonly-token }}"]


def test_e2e_tests_the_installed_tool_version():
    """No ref input and no default repository: e2e fetches the source the tool was installed from."""
    deploy = _load()["jobs"]["deploy"]
    fetch = _step_named(deploy, "Fetch the installed tool's tests")
    assert f"{WRAPPER} tool-source" in fetch["run"]
    assert 'checkout --quiet --detach "$commit"' in fetch["run"]
    e2e = _step_named(deploy, "e2e")
    assert e2e["working-directory"] == "${{ steps.tool.outputs.dir }}"
    assert e2e["env"]["TESTS"] == "${{ steps.tool.outputs.tests }}"
    assert not _step_using(deploy, "actions/checkout")[1:], "no second checkout of a fixed ref"


def test_no_ssh_key_step():
    """Both repositories are public and the dependency is an https URL: nothing needs a key."""
    text = REUSABLE.read_text()
    assert "ssh" not in text.lower()


def test_deploy_and_refresh_use_separate_job_level_concurrency_groups():
    """A refresh must not cancel a deploy waiting for approval."""
    wf = _load()
    # One shared group lets the daily refresh cancel a deploy that waits for approval.
    assert "concurrency" not in wf
    jobs = wf["jobs"]
    deploy, refresh = jobs["deploy"]["concurrency"], jobs["refresh"]["concurrency"]
    assert deploy["group"] != refresh["group"]
    # Each group carries the config input, so two configs in one repo do not share one.
    assert deploy["group"] == "sync-deploy-${{ inputs.config }}"
    assert refresh["group"] == "sync-refresh-${{ inputs.config }}"
    assert deploy["cancel-in-progress"] is False
    assert refresh["cancel-in-progress"] is False
    assert "concurrency" not in jobs["plan"]


def _tool_invocations(job: dict) -> list[list[str]]:
    """Every `gitops-tool ...` argument list written in a job's run scripts.

    Handles one-line calls (also inside `$(...)`) and the `args=(...)` / `args+=(...)`
    array the apply step builds before `gitops-tool "${args[@]}"`. Values stay as written
    (`$SNAPSHOT`, `${BEFORE:-}`); only the shape is checked.
    """
    calls: list[list[str]] = []
    array: list[str] = []
    for line in _runs(job).splitlines():
        line = line.strip()
        if line.startswith(("#", "echo")):
            continue
        built = re.fullmatch(r"args\+?=\((.*)\)", line)
        if built:
            array += shlex.split(built.group(1))
            continue
        for found in re.finditer(rf"\b{WRAPPER} ([a-z][a-z-]*[^\n)|;&]*)", line):
            calls.append(shlex.split(found.group(1)))
    if array:
        calls.append(array)
    return calls


def test_workflow_calls_only_contract_commands():
    """Every tool call is a contract subcommand with contract flags and contract config keys."""
    seen: set[str] = set()
    for name, job in _load()["jobs"].items():
        for argv in _tool_invocations(job):
            cmd, rest = argv[0], argv[1:]
            assert cmd in CONTRACT, (
                f"{name}: `{WRAPPER} {' '.join(argv)}` is not a contract command"
            )
            flags = {a for a in rest if a.startswith("--")}
            assert flags <= CONTRACT[cmd], f"{name}: {cmd} passes {flags - CONTRACT[cmd]}"
            if cmd == "config-get":
                assert rest and rest[0] in CONFIG_KEYS | {"$key"}, f"{name}: {argv}"
            seen.add(cmd)
    # Guards the scan itself: it must find the calls the workflow is known to make.
    assert seen == set(CONTRACT)


def test_config_keys_the_workflow_reads_are_contract_keys():
    """The loop that reads the directories names only contract keys."""
    wf = _load()
    locate = _step_named(wf["jobs"]["deploy"], "Locate the directories")["run"]
    loop = re.search(r"for key in ([a-z_ ]+); do", locate)
    assert loop and set(loop.group(1).split()) <= CONFIG_KEYS


def test_tokens_reach_only_the_steps_that_use_them():
    wf = _load()
    for name, job in wf["jobs"].items():
        assert "secrets" not in str(job.get("env", {})), f"{name} puts a secret at job level"
        for step in job["steps"]:
            env = step.get("env", {})
            if any("secrets.destination" in str(v) for v in env.values()):
                assert "GITOPS_TOKEN" in env
                assert set(env) <= {"GITOPS_TOKEN", "MODE", "SNAPSHOT", "ROWS", "TESTS"}
                assert re.search(
                    r"sync-inbound|deploy --apply|restore --from|check|refresh|run-live",
                    step["run"],
                ), step.get("name")


def test_secrets_are_static_names():
    """Dynamic `secrets[...]` lookups depended on `inherit`; every secret is declared."""
    text = REUSABLE.read_text()
    assert "secrets[" not in text and "vars[" not in text
    used = set(re.findall(r"secrets\.([a-z-]+)", text))
    assert used == SECRETS


def test_no_expressions_inside_run_scripts():
    """Inputs and outputs reach scripts through `env:`, so a crafted input cannot inject."""
    for job in _load()["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name")


def test_every_action_is_pinned_to_a_sha():
    for job in _load()["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", step["uses"]), step


def test_workflow_names_no_owner():
    assert "markddavidoff" not in REUSABLE.read_text()


# --- the scripts themselves, run under bash with stubs -------------------------------


def _run_step(step: dict, tmp_path: Path, env: dict[str, str]) -> tuple[int, dict, str]:
    out = tmp_path / "github_output"
    out.write_text("")
    full_env = {
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_ENV": str(tmp_path / "github_env"),
        "GITHUB_PATH": str(tmp_path / "github_path"),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        **env,
    }
    (tmp_path / "runner").mkdir(exist_ok=True)
    proc = subprocess.run(
        ["bash", "-c", step["run"]], env=full_env, capture_output=True, text=True, check=False
    )
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return proc.returncode, outputs, proc.stderr


def _stub_tool(tmp_path: Path, changed_output: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "tool-calls"
    stub = bin_dir / WRAPPER
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\necho {changed_output}\n')
    stub.chmod(0o755)
    return calls


@pytest.mark.parametrize(
    ("event", "mode_input", "changed", "want_mode", "want_run"),
    [
        ("schedule", "", "changed", "refresh", "true"),
        ("schedule", "deploy", "changed", "refresh", "true"),
        ("workflow_dispatch", "", "unchanged", "deploy", "true"),
        ("workflow_dispatch", "e2e", "unchanged", "e2e", "true"),
        ("workflow_dispatch", "restore", "unchanged", "restore", "true"),
        ("workflow_dispatch", "refresh", "unchanged", "refresh", "true"),
        ("push", "", "changed", "deploy", "true"),
        ("push", "", "unchanged", "deploy", "false"),
    ],
)
def test_plan_decides_mode_and_run(tmp_path, event, mode_input, changed, want_mode, want_run):
    decide = _step_named(_load()["jobs"]["plan"], "Decide the mode")
    calls = _stub_tool(tmp_path, changed)
    code, outputs, _ = _run_step(
        decide, tmp_path, {"EVENT": event, "MODE_INPUT": mode_input, "BEFORE": "abc123"}
    )
    assert code == 0
    assert outputs == {"mode": want_mode, "run": want_run}
    # Only a push asks `changed`; dispatch and schedule always run their mode.
    asked = calls.exists() and "changed --since abc123" in calls.read_text()
    assert asked == (event == "push")


def test_plan_rejects_an_unknown_mode(tmp_path):
    decide = _step_named(_load()["jobs"]["plan"], "Decide the mode")
    _stub_tool(tmp_path, "changed")
    code, outputs, err = _run_step(
        decide, tmp_path, {"EVENT": "workflow_dispatch", "MODE_INPUT": "nuke", "BEFORE": ""}
    )
    assert code == 2
    assert "unknown mode 'nuke'" in err
    assert outputs == {}


def _stub_uv(tmp_path: Path) -> Path:
    """A `uv` that records its arguments and names a tool bin dir holding a fake command."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    tool_bin = tmp_path / "tool-bin"
    tool_bin.mkdir(exist_ok=True)
    command = tool_bin / "my-tool"
    command.write_text('#!/bin/sh\necho "args=$*"\necho "token=${SECRET_VAR:-}"\n')
    command.chmod(0o755)
    calls = tmp_path / "uv-calls"
    uv = bin_dir / "uv"
    uv.write_text(
        f'#!/bin/sh\necho "$@" >> "{calls}"\n'
        f'if [ "$1 $2 $3" = "tool dir --bin" ]; then echo "{tool_bin}"; fi\n'
    )
    uv.chmod(0o755)
    return calls


def _install_env(**over: str) -> dict[str, str]:
    base = {
        "TOOL": "my-tool @ git+https://example.invalid/owner/my-tool.git@v1",
        "COMMAND": "my-tool",
        "CONFIG": "sync.yaml",
        "TOKEN_ENV": "SECRET_VAR",
    }
    return {**base, **over}


def test_install_step_installs_the_tool_and_writes_a_wrapper(tmp_path):
    install = _step_named(_load()["jobs"]["plan"], "Install the tool")
    uv_calls = _stub_uv(tmp_path)
    code, _, err = _run_step(install, tmp_path, _install_env())
    assert code == 0, err
    assert "tool install" in uv_calls.read_text()
    assert "my-tool @ git+https://example.invalid/owner/my-tool.git@v1" in uv_calls.read_text()
    env_file = (tmp_path / "github_env").read_text()
    assert "GITOPS_COMMAND=my-tool" in env_file
    assert "GITOPS_CONFIG=sync.yaml" in env_file
    assert "GITOPS_TOKEN_ENV=SECRET_VAR" in env_file

    # The wrapper passes --config, exports the token under the name the tool reads, and
    # drops the neutral variable before it execs the tool.
    wrapper = tmp_path / "runner" / "bin" / WRAPPER
    path_dirs = (tmp_path / "github_path").read_text().split()
    run_env = {
        **os.environ,
        "PATH": os.pathsep.join([*path_dirs, os.environ["PATH"]]),
        "GITOPS_COMMAND": "my-tool",
        "GITOPS_CONFIG": "sync.yaml",
        "GITOPS_TOKEN_ENV": "SECRET_VAR",
        "GITOPS_TOKEN": "s3cret",
    }
    proc = subprocess.run(
        [str(wrapper), "check"], env=run_env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert "args=--config sync.yaml check" in proc.stdout
    assert "token=s3cret" in proc.stdout


@pytest.mark.parametrize(
    "over",
    [
        {"TOKEN_ENV": "BAD NAME"},
        {"TOKEN_ENV": "1BAD"},
        {"TOKEN_ENV": "A=b"},
        {"COMMAND": "../evil"},
        {"COMMAND": "a b"},
        {"CONFIG": "one\ntwo"},
    ],
)
def test_install_step_rejects_unsafe_inputs(tmp_path, over):
    install = _step_named(_load()["jobs"]["plan"], "Install the tool")
    uv_calls = _stub_uv(tmp_path)
    code, _, err = _run_step(install, tmp_path, _install_env(**over))
    assert code == 2, err
    assert not uv_calls.exists(), "nothing is installed from a rejected input"
