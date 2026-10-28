"""The nightly index export job template (M2-44, D39; spec §10.3): builds once, publishes
outside `main` (an artifact, or the `agentic-index` ref), never commits, passes
actionlint, and its dry run writes nothing anywhere."""

import os
import re
import shlex
import shutil
import subprocess
from importlib.resources import files

import pytest
from cli_helpers import git

from agentic.codeintel.index import available
from agentic.yamlio import load_yaml
from agentic_cli import repo_setup
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

TEMPLATE = files("agentic_cli").joinpath("templates", "agentic-index-export.yml")
ALLOWED_ACTIONS = {
    "actions/checkout",
    "actions/upload-artifact",
    "actions/download-artifact",
    "astral-sh/setup-uv",
}
SOURCES = {
    "pkg/__init__.py": "",
    "pkg/models.py": "class Model:\n    def name(self):\n        return 'model'\n",
}


@pytest.fixture(scope="module")
def workflow() -> dict:
    data, _ = load_yaml(TEMPLATE.read_text(encoding="utf-8"))
    return data


def steps(workflow: dict):
    for name, job in workflow["jobs"].items():
        for step in job["steps"]:
            yield name, step


def test_init_repo_proposes_exactly_this_template():
    assert repo_setup.INDEX_EXPORT_WORKFLOW == TEMPLATE.read_text(encoding="utf-8")
    assert repo_setup.WORKFLOW_REL == ".github/workflows/agentic-index-export.yml"


def test_it_runs_nightly_and_by_hand_with_a_dry_run(workflow):
    assert workflow["on"]["schedule"][0]["cron"].split()[0] not in ("0", "*")
    dry_run = workflow["on"]["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean" and dry_run["default"] is False
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_the_workflow_never_writes_main(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    writers = {
        name: job
        for name, job in workflow["jobs"].items()
        if job.get("permissions", {}).get("contents") == "write"
    }
    assert list(writers) == ["publish-ref"]
    assert "vars.AGENTIC_INDEX_PUBLISH == 'ref'" in writers["publish-ref"]["if"]
    for job, step in steps(workflow):
        uses = step.get("uses")
        if uses:
            assert uses.split("@")[0] in ALLOWED_ACTIONS, (job, uses)
        run = step.get("run", "")
        assert not re.search(r"\bgit\s+(push|commit|tag|update-ref|merge)\b", run), (job, run)
        for line in run.splitlines():
            if line.strip().startswith("agentic"):
                words = shlex.split(line)
                verb = [w for w in words[1:] if not w.startswith("-")][:2]
                assert verb in (["index", "export"], ["index", "publish"]), line
                if verb == ["index", "publish"]:
                    assert words[words.index("--ref") + 1] == "agentic-index"


def test_the_export_job_keeps_no_token_and_only_reads(workflow):
    job = workflow["jobs"]["export"]
    assert "permissions" not in job  # the workflow's contents: read
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/c"))
    assert checkout["with"]["persist-credentials"] is False


def test_a_dry_run_artifact_is_never_the_one_developers_import(workflow):
    upload = next(s for _, s in steps(workflow) if s.get("uses", "").startswith("actions/up"))
    download = next(s for _, s in steps(workflow) if s.get("uses", "").startswith("actions/do"))
    assert upload["with"]["name"] == download["with"]["name"]
    assert "agentic-index-export-dry-run" in upload["with"]["name"]
    assert "gh run download -n agentic-index-export" in TEMPLATE.read_text(encoding="utf-8")


def test_the_header_documents_the_token_the_job_needs():
    header = TEMPLATE.read_text(encoding="utf-8").split("\nname:", 1)[0]
    for needed in ("GITHUB_TOKEN", "contents: read", "contents: write", "run.trigger"):
        assert needed in header, needed
    assert "service-account token" in header and "NEVER MAIN" in header


# ── the job's own commands, run against a local remote ───────────────────────


def job_commands(workflow: dict, env: dict[str, str]) -> list[list[str]]:
    """The `agentic` commands of the export and publish-ref jobs, in order, with the
    step's environment and `$RUNNER_TEMP` expanded as the runner's shell would."""
    commands = []
    for _, step in steps(workflow):
        expanded = {**env}
        for key, value in (step.get("env") or {}).items():
            expanded[key] = env.get(key, "") if "${{" in str(value) else str(value)
        for line in step.get("run", "").splitlines():
            if not line.strip().startswith("agentic"):
                continue
            commands.append(shlex.split(expand(line, expanded), posix=True)[1:])
    return commands


def expand(line: str, env: dict[str, str]) -> str:
    """`${NAME:+word}` and `$NAME`, as a POSIX shell expands them."""
    line = re.sub(r"\$\{(\w+):\+([^}]*)\}", lambda m: m[2] if env.get(m[1]) else "", line)
    return re.sub(r"\$(\w+)", lambda m: env[m[1]], line)


@pytest.mark.skipif(not available(), reason="needs the codeintel extra")
def test_the_jobs_dry_run_writes_nothing_and_a_real_run_only_agentic_index(
    workflow, tmp_path, monkeypatch, home
):
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", "Index Bot")
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "index-bot@example.invalid")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", str(remote), str(checkout))
    for rel, text in SOURCES.items():
        (checkout / rel).parent.mkdir(parents=True, exist_ok=True)
        (checkout / rel).write_bytes(text.encode("utf-8"))
    git(checkout, "add", "-A")
    git(checkout, "commit", "-qm", "sources")
    git(checkout, "push", "-q", "origin", "main")
    monkeypatch.chdir(checkout)

    def remote_refs() -> str:
        return git(remote, "for-each-ref", "--format=%(refname) %(objectname)")

    before = remote_refs()
    for dry_run in ("1", ""):
        temp = tmp_path / f"runner-temp-{dry_run or 'real'}"
        temp.mkdir()
        commands = job_commands(workflow, {"RUNNER_TEMP": temp.as_posix(), "DRY_RUN": dry_run})
        assert [c[:2] for c in commands] == [["index", "export"], ["index", "publish"]]
        for command in commands:
            assert main(["--json", *command]) == ExitCode.OK, command
        if dry_run:
            assert remote_refs() == before  # the dry run wrote nothing at all
    added = set(remote_refs().splitlines()) - set(before.splitlines())
    assert [line.split()[0] for line in added] == ["refs/heads/agentic-index"]
    assert set(before.splitlines()) <= set(remote_refs().splitlines())  # main unchanged


# ── actionlint ───────────────────────────────────────────────────────────────


def test_the_template_passes_actionlint(tmp_path):
    found = shutil.which("actionlint")
    if found is None:
        if os.environ.get("AGENTIC_CHECK_IMAGE"):
            pytest.fail("the Linux check image must carry the pinned actionlint")
        pytest.skip(
            "actionlint is a native binary: it runs only in the Linux check container "
            "(scripts/check.py --linux), never on a developer laptop"
        )
    target = tmp_path / ".github" / "workflows" / "agentic-index-export.yml"
    target.parent.mkdir(parents=True)
    target.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    done = subprocess.run(
        [found, "-no-color", str(target)], capture_output=True, text=True, cwd=tmp_path
    )
    assert done.returncode == 0, done.stdout + done.stderr
