"""validate, agents|pipelines|skills list/search, agents new, pipelines new, policy show (M1-56)."""

from pathlib import Path

from _cli_support import write

from agentic.definitions import load_agent, load_pipeline
from agentic_cli.exit_codes import ExitCode

ROOT = Path(__file__).resolve().parents[3]  # tests/run/ is two folders below cli/
TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "agentic_cli" / "templates"


def test_validate_passes_on_valid_definitions(workspace, cli):
    result = cli("validate")
    assert result.code == ExitCode.OK, result.err
    assert "2 agents" in result.out and "1 pipeline" in result.out


def test_validate_reports_policy_violations_with_field_names(workspace, cli):
    write(
        workspace / ".agentic" / "agents" / "greedy.md",
        "---\nname: greedy\ntype: general\nversion: 0.1.0\ndescription: Wants too much.\n"
        "tier: deep-thought\ntools: [read_files, run_shell]\n---\nHi.\n",
    )
    result = cli("--json", "validate")
    assert result.code == ExitCode.INVALID
    problems = result.json()["problems"]
    fields = {(p["name"], p["field"]) for p in problems}
    assert ("greedy", "tier") in fields and ("greedy", "tools") in fields
    tier = next(p for p in problems if p["field"] == "tier")
    assert tier["allowed"] == ["balanced", "fast"] and tier["reason"]

    human = cli("validate")
    assert human.code == ExitCode.INVALID
    assert "greedy: tier: unknown tier" in human.out
    assert "tools: " in human.out


def test_validate_names_the_file_a_problem_is_in(workspace, cli):  # F5 (b)
    path = write(
        workspace / ".agentic" / "agents" / "greedy.md",
        "---\nname: greedy\ntype: general\nversion: 0.1.0\ndescription: Wants too much.\n"
        "tier: deep-thought\n---\nHi.\n",
    )
    problems = cli("--json", "validate").json()["problems"]
    assert {p["file"] for p in problems if p["name"] == "greedy"} == {str(path)}
    assert f"{path}: greedy: tier" in cli("validate").out


CONDITIONAL = """name: cond
version: 1.0.0
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: architect
    when: "requirements.status == 'complete'"
  - id: build
    agent: dev
"""


def test_validate_passes_with_warnings_only(workspace, policy_repo, cli):  # F5 (a)
    write(
        policy_repo / "agents" / "dev.md",
        "---\nname: dev\ntype: general\nversion: 1.0.0\ndescription: Dev.\n"
        "consumes: [design]\n---\nBuild.\n",
    )
    path = write(policy_repo / "pipelines" / "cond.yaml", CONDITIONAL)
    dry = cli("--json", "run", "cond", "--dry-run")
    assert dry.code == ExitCode.OK, dry.out
    assert any(p["severity"] == "warning" for p in dry.json()["preflight"]["problems"])

    result = cli("--json", "validate")
    assert result.code == ExitCode.OK, result.out + result.err
    warnings = [p for p in result.json()["problems"] if p["severity"] == "warning"]
    assert warnings and {p["file"] for p in warnings} == {str(path)}
    human = cli("validate")
    assert human.code == ExitCode.OK and "1 warning" in human.out


def test_validate_one_file_reports_load_errors_with_lines(workspace, cli):
    broken = write(
        workspace / "draft.md",
        "---\nname: draft\ntype: general\nversion: 0.1.0\ndescription: x\n"
        "limits: { max_turns: lots }\n---\nHi.\n",
    )
    result = cli("validate", str(broken))
    assert result.code == ExitCode.INVALID
    assert "limits.max_turns" in result.out and "draft.md" in result.out
    assert "(line 6)" in result.out


def test_validate_a_pipeline_runs_preflight(workspace, cli):
    pipeline = write(
        workspace / "p.yaml",
        "name: p\nversion: 1.0.0\nsteps:\n  - id: one\n    agent: nobody\n",
    )
    result = cli("--json", "validate", str(pipeline))
    assert result.code == ExitCode.INVALID
    assert any("nobody" in p["reason"] for p in result.json()["problems"])


def test_agents_list_and_search(workspace, cli):
    listed = cli("--json", "agents", "list")
    assert listed.code == ExitCode.OK
    rows = listed.json()
    assert [r["name"] for r in rows] == ["architect", "ba"]
    assert rows[0] == {
        "name": "architect",
        "type": "designer",
        "version": "2.1.0",
        "layer": "org",
        "description": "Designs.",
        "status": "active",
    }
    found = cli("--json", "agents", "search", "ANALYST")
    assert [r["name"] for r in found.json()] == ["ba"]
    assert "architect" in cli("agents", "list").out


def test_pipelines_list_and_search(workspace, cli):
    rows = cli("--json", "pipelines", "list").json()
    assert rows == [
        {"name": "deliver", "version": "1.0.0", "regime": "team", "layer": "org", "steps": 3}
    ]
    assert cli("--json", "pipelines", "search", "nothing").json() == []


def test_skills_list_and_search_read_the_policy_catalog(workspace, policy_repo, cli):
    library = ROOT / "library" / "skills"
    write(policy_repo / "skills" / "catalog.yaml", (library / "catalog.yaml").read_text("utf-8"))
    rows = cli("--json", "skills", "list").json()
    keys = [r["key"] for r in rows]
    assert "superpowers/test-driven-development" in keys
    assert all(r["installed"] is False for r in rows)
    found = cli("--json", "skills", "search", "debugging").json()
    assert [r["key"] for r in found] == ["superpowers/systematic-debugging"]


def test_skills_list_without_a_catalog_says_so(workspace, cli):
    result = cli("skills", "list")
    assert result.code == ExitCode.OK and "no skills catalog" in result.err


def test_agents_new_starts_from_the_type_with_name_and_type_filled(workspace, cli):
    result = cli("agents", "new", "doc-writer", "--type", "general")
    assert result.code == ExitCode.OK, result.err
    path = workspace / ".agentic" / "agents" / "doc-writer.md"
    agent = load_agent(path)
    assert (agent.name, agent.type) == ("doc-writer", "general")
    assert cli("validate", str(path)).code == ExitCode.OK
    again = cli("agents", "new", "doc-writer", "--type", "general")
    assert again.code == ExitCode.REFUSED and "exists" in again.err


def test_agents_new_uses_the_org_type_template_when_there_is_one(workspace, policy_repo, cli):
    write(
        policy_repo / "templates" / "agents" / "analyst.md",
        "---\nname: my-agent\ntype: analyst\nversion: 0.1.0\ndescription: Org analyst.\n"
        "---\nOrg template body.\n",
    )
    assert cli("agents", "new", "scout", "--type", "analyst").code == ExitCode.OK
    text = (workspace / ".agentic" / "agents" / "scout.md").read_text("utf-8")
    assert "Org template body." in text and "name: scout" in text


def test_agents_new_writes_nothing_from_a_broken_org_template(workspace, policy_repo, cli):
    """A file that does not load would break every later command in the repo (DF-220)."""
    write(
        policy_repo / "templates" / "agents" / "analyst.md",
        "---\nname: my-agent\ntype: analyst\nversion: not-a-version\nno_such_field: 1\n"
        "---\nOrg template body.\n",
    )
    result = cli("agents", "new", "scout", "--type", "analyst")
    assert result.code == ExitCode.INVALID, result.err
    assert "templates" in result.err and "analyst.md" in result.err
    assert not (workspace / ".agentic" / "agents" / "scout.md").exists()


def test_agents_new_refuses_an_unknown_type_or_a_bad_name(workspace, cli):
    unknown = cli("agents", "new", "x", "--type", "wizard")
    assert unknown.code == ExitCode.INVALID and "analyst" in unknown.err
    bad = cli("agents", "new", "Bad Name", "--type", "general")
    assert bad.code == ExitCode.USAGE


def test_pipelines_new_copies_the_template(workspace, cli):
    result = cli("pipelines", "new", "my-flow")
    assert result.code == ExitCode.OK, result.err
    pipeline = load_pipeline(workspace / ".agentic" / "pipelines" / "my-flow.yaml")
    assert pipeline.name == "my-flow"


def test_pipelines_new_warns_when_org_policy_ignores_repo_pipelines(workspace, policy_repo, cli):
    write(policy_repo / "policy" / "org.yaml", "allow_repo_pipelines: false\n")
    result = cli("pipelines", "new", "my-flow")
    assert result.code == ExitCode.OK and "allow_repo_pipelines" in result.err


def test_an_ignored_repo_pipeline_is_named_where_people_look(workspace, policy_repo, cli):
    """`pipelines new` warns once; later a run of it says 'no pipeline named' unless the
    reason is shown with it (DF-222 f)."""
    write(policy_repo / "policy" / "org.yaml", "allow_repo_pipelines: false\n")
    assert cli("pipelines", "new", "my-flow").code == ExitCode.OK
    for args in (["pipelines", "list"], ["validate"], ["run", "my-flow", "--dry-run"]):
        result = cli(*args)
        assert "ignored repo pipeline my-flow.yaml" in result.err, (args, result.err)


def test_policy_show(workspace, cli):
    shown = cli("--json", "policy", "show").json()
    assert shown["tiers"]["balanced"]["model"] == "acme/model-b"
    assert shown["agent_types"]["analyst"]["caps"]["budget_usd"] == 2
    assert shown["team"]["name"] == "payments"
    assert shown["version"].startswith(("git:", "sha256:"))
    human = cli("policy", "show").out
    assert "acme/model-b" in human and "analyst" in human


def test_the_packaged_templates_equal_the_repository_templates():
    for packaged, original in (
        ("agent.md", ROOT / "templates" / "agent" / "agent.md"),
        ("pipeline.yaml", ROOT / "templates" / "pipeline" / "pipeline.yaml"),
    ):
        assert (TEMPLATES / packaged).read_bytes() == original.read_bytes(), packaged
