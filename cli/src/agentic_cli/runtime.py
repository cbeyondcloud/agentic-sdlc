"""The engine for one workspace (M1-54, DF-47): what `run`, `resume` and the gate verbs
drive. Built in Task 4; the constants here are shared with `validate`."""

from pathlib import Path

from agentic_cli.workspace import Workspace

#: The backends this CLI builds. The claude-code backend is not wired yet (DF-74), so
#: preflight refuses a step that names it instead of failing mid-run.
CONFIGURED_BACKENDS: tuple[str, ...] = ("native",)
#: Built-in actions (`github.open_pr` arrives with M1-58).
ACTIONS: dict = {}
SKILLS = Path(".agentic") / "local" / "skills"


def installed_skills_root(ws: Workspace) -> Path:
    """Where `agentic init` installs the repo's skills (git-ignored)."""
    return ws.repo / SKILLS
