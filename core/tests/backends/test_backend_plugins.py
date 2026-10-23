"""The full superpowers plugin as a team opt-in (M2-47, D28, spec §24 "Superpowers"):
pinned in the org catalog, installed only into a claude-code or codex step's own config
folder, never into the native loop, with the visual companion (and any telemetry) out."""

import tomllib
from decimal import Decimal
from pathlib import Path

import pytest
from conformance import slice_

from agentic.backends.capabilities import CAPABILITIES
from agentic.backends.claude_code import ClaudeCodeSettings
from agentic.backends.claude_code import lock_down as claude_lock_down
from agentic.backends.codex import CodexError, CodexSettings
from agentic.backends.codex import lock_down as codex_lock_down
from agentic.backends.codex_rules import ShellRules
from agentic.engine.engine import StepContext
from agentic.policy.model import TeamPolicy
from agentic.skills import Catalog, load_catalog, skill_digest
from agentic.skills.plugins import install_plugin, plugins_for
from agentic.transport import BudgetLedger, CallIdentity

LIBRARY = Path(__file__).resolve().parents[3] / "library" / "skills"
ORCHESTRATION = (
    "using-superpowers",
    "subagent-driven-development",
    "executing-plans",
    "using-git-worktrees",
    "finishing-a-development-branch",
)
IDENTITY = CallIdentity(
    team="payments",
    user="dev@acme.example",
    repo="acme/app",
    pipeline="feature-delivery",
    run_id="r-1",
    step="develop",
    agent="developer@1.2.0",
    policy_version="v1",
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_plugin(root: Path, *, extra: dict[str, str] | None = None) -> Path:
    """A small stand-in for the vendored superpowers plugin under `<root>/plugins/`."""
    folder = root / "plugins" / "superpowers"
    files = {
        ".claude-plugin/plugin.json": '{"name": "superpowers", "version": "5.0.0"}\n',
        "skills/using-superpowers/SKILL.md": "---\nname: using-superpowers\n"
        "description: Use at the start.\n---\n\nCheck for skills first.\n",
        "skills/executing-plans/SKILL.md": "---\nname: executing-plans\n"
        "description: Use with a plan.\n---\n\nFollow the plan.\n",
        "skills/brainstorming/SKILL.md": "---\nname: brainstorming\n"
        "description: Use before building.\n---\n\nAsk questions.\n",
        "skills/brainstorming/visual-companion/server.js": "fetch('https://example.com/x')\n",
        "LICENSE": "MIT License\n",
        **(extra or {}),
    }
    for rel, text in files.items():
        write(folder / rel, text)
    return folder


def catalog_with(folder: Path, **changes) -> Catalog:
    entry = {
        "source": "https://github.com/obra/superpowers",
        "commit": "b" * 40,
        "licence": "MIT",
        "owner": "platform-team",
        "version": "5.0.0",
        "sha256": skill_digest(folder),
        "scripts_reviewed_by": "security-team",
        "exclude": ["skills/brainstorming/visual-companion/**"],
        **changes,
    }
    return Catalog.model_validate({"plugins": {"superpowers": entry}})


def team(on: bool = True) -> TeamPolicy:
    return TeamPolicy(backend_plugins=["superpowers"] if on else [])


def agent_on(resolved, backend: str):
    return resolved.model_copy(update={"backend": backend, "temperature": None})


# ── which steps get it ───────────────────────────────────────────────────────


def test_plugin_only_when_the_team_flag_is_on(resolved, tmp_path: Path):
    folder = make_plugin(tmp_path)
    catalog = catalog_with(folder)
    agent = agent_on(resolved, "claude-code")
    assert [p.name for p in plugins_for(agent, team(True), catalog, tmp_path)] == ["superpowers"]
    assert plugins_for(agent, team(False), catalog, tmp_path) == []
    assert plugins_for(agent, None, catalog, tmp_path) == []


def test_native_backend_never_gets_plugins(resolved, tmp_path: Path):
    folder = make_plugin(tmp_path)
    assert not CAPABILITIES["native"].plugins
    assert CAPABILITIES["claude-code"].plugins and CAPABILITIES["codex"].plugins
    native = agent_on(resolved, "native")
    assert plugins_for(native, team(True), catalog_with(folder), tmp_path) == []


def test_a_plugin_limited_to_some_backends(resolved, tmp_path: Path):
    folder = make_plugin(tmp_path)
    catalog = catalog_with(folder, backends=["claude-code"])
    assert plugins_for(agent_on(resolved, "codex"), team(), catalog, tmp_path) == []


def test_hash_mismatch_refused_and_recorded(resolved, tmp_path: Path):
    folder = make_plugin(tmp_path)
    catalog = catalog_with(folder)
    write(folder / "skills" / "brainstorming" / "SKILL.md", "edited\n")
    seen = []
    found = plugins_for(
        agent_on(resolved, "claude-code"),
        team(),
        catalog,
        tmp_path,
        record=lambda event, fields: seen.append((event, fields)),
    )
    assert found == []
    assert seen[0][0] == "skills.plugin_refused" and "hash" in seen[0][1]["reason"]


def test_telemetry_url_refuses_the_plugin(resolved, tmp_path: Path):
    folder = make_plugin(tmp_path, extra={"hooks/report.md": "POST to https://x.io/telemetry\n"})
    seen = []
    found = plugins_for(
        agent_on(resolved, "claude-code"),
        team(),
        catalog_with(folder),
        tmp_path,
        record=lambda event, fields: seen.append((event, fields)),
    )
    assert found == [] and "telemetry" in seen[0][1]["reason"]


def test_excluded_files_are_not_scanned_or_installed(resolved, tmp_path: Path):
    folder = make_plugin(
        tmp_path,
        extra={
            "skills/brainstorming/visual-companion/t.md": ("send usage to https://x.io/telemetry\n")
        },
    )
    [plugin] = plugins_for(
        agent_on(resolved, "claude-code"), team(), catalog_with(folder), tmp_path
    )
    target = install_plugin(plugin, tmp_path / "step")
    assert (target / "skills" / "brainstorming" / "SKILL.md").is_file()
    assert not (target / "skills" / "brainstorming" / "visual-companion").exists()


def test_a_plugin_needs_a_script_reviewer_and_a_known_name():
    with pytest.raises(ValueError):
        Catalog.model_validate(
            {
                "plugins": {
                    "superpowers": {
                        "source": "x",
                        "commit": "c",
                        "licence": "MIT",
                        "owner": "o",
                        "version": "1",
                        "sha256": "sha256:" + "0" * 64,
                    }
                }
            }
        )


# ── where it is installed ────────────────────────────────────────────────────


def claude_step(agent) -> StepContext:
    budget = BudgetLedger(Decimal("10")).run.open("develop-1", "step", Decimal("2"))
    return StepContext("r-1", "develop", 1, agent, budget, None)


def test_claude_code_options_carry_the_local_plugin_in_the_step_config_dir(resolved, tmp_path):
    folder = make_plugin(tmp_path / "catalog")
    agent = agent_on(resolved, "claude-code")
    [plugin] = plugins_for(agent, team(), catalog_with(folder), tmp_path / "catalog")
    settings = ClaudeCodeSettings("https://tokenlean.example", "tl-key", tmp_path / "state")
    kwargs = {"settings": settings, "identity": IDENTITY, "permissions": {}}
    options = claude_lock_down(
        claude_step(agent), cwd=tmp_path, edit_tree=False, plugins=[plugin], **kwargs
    )
    [loaded] = options["plugins"]
    path = Path(loaded["path"])
    assert loaded["type"] == "local"
    assert path.is_relative_to(Path(options["env"]["CLAUDE_CONFIG_DIR"]))
    assert (path / ".claude-plugin" / "plugin.json").is_file()
    assert not (path / "skills" / "brainstorming" / "visual-companion").exists()
    plain = claude_lock_down(claude_step(agent), cwd=tmp_path, edit_tree=False, **kwargs)
    assert "plugins" not in plain


def codex_launch(resolved, tmp_path: Path, plugins, tree: Path | None = None):
    agent = agent_on(resolved, "codex")
    tree = tree or tmp_path / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    return codex_lock_down(
        StepContext("r-1", "develop", 2, agent, slice_(), None),
        settings=CodexSettings("https://tokenlean.example", "tl-key", tmp_path / "homes"),
        identity=IDENTITY,
        rules=ShellRules(allow=(), ask=(), deny=()),
        cwd=tree,
        srt_boundary=True,
        environ={"PATH": "/usr/bin"},
        plugins=plugins,
    )


def test_codex_home_gets_the_plugin_skills_and_the_plugins_feature_stays_off(resolved, tmp_path):
    folder = make_plugin(tmp_path / "catalog")
    agent = agent_on(resolved, "codex")
    plugins = plugins_for(agent, team(), catalog_with(folder), tmp_path / "catalog")
    launch = codex_launch(resolved, tmp_path, plugins)
    launch.write("Be the developer.")
    config = tomllib.loads((launch.home / "config.toml").read_text(encoding="utf-8"))
    assert config["features"]["plugins"] is False  # no chatgpt.com/github.com fetches (DF-118)
    assert config["skills"]["include_instructions"] is True
    skills = sorted(p.name for p in (launch.home / "skills").iterdir())
    assert skills == ["brainstorming", "executing-plans", "using-superpowers"]
    assert not (launch.home / "skills" / "brainstorming" / "visual-companion").exists()
    plain = codex_launch(resolved, tmp_path / "plain", [])
    plain.write("Be the developer.")
    config = tomllib.loads((plain.home / "config.toml").read_text(encoding="utf-8"))
    assert config["skills"]["include_instructions"] is False
    assert not (plain.home / "skills").exists()


@pytest.mark.parametrize("where", [".agents/skills/x/SKILL.md", ".codex/skills/x/SKILL.md"])
def test_codex_refuses_a_plugin_beside_repo_skill_folders(resolved, tmp_path, where):
    """With skill instructions on, Codex would list the repo's own skills too."""
    folder = make_plugin(tmp_path / "catalog")
    plugins = plugins_for(
        agent_on(resolved, "codex"), team(), catalog_with(folder), tmp_path / "catalog"
    )
    tree = tmp_path / "tree"
    write(tree / where, "---\nname: x\ndescription: y\n---\n")
    with pytest.raises(CodexError, match="repo"):
        codex_launch(resolved, tmp_path, plugins, tree=tree)


# ── the native loop ──────────────────────────────────────────────────────────


def test_library_catalog_excludes_orchestration_skills():
    catalog = load_catalog(LIBRARY)
    for name in ORCHESTRATION:
        assert name in catalog.excluded, name
        assert not any(key.split("/")[1] == name for key in catalog.skills), name
    entry = catalog.skills["superpowers/test-driven-development"].model_dump()
    smuggled = {"skills": {"superpowers/executing-plans": entry}, "excluded": catalog.excluded}
    with pytest.raises(ValueError, match="excluded"):
        Catalog.model_validate(smuggled)


def test_plugins_are_not_catalogue_skills(tmp_path: Path):
    """The plugin folder sits beside the skills, and admission does not mistake it for an
    uncatalogued skill source."""
    from agentic.skills.admission import admit_catalog

    make_plugin(tmp_path)
    write(tmp_path / "catalog.yaml", "skills: {}\n")
    assert admit_catalog(tmp_path) == {}
