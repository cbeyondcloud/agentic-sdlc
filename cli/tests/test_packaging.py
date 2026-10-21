"""The CLI's package metadata declares every distribution its code imports, so an
installed CLI never works only by accident of another package's dependencies (DF-206)."""

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1]
#: imported only behind a check that the optional telemetry extra is installed
OPTIONAL = {"opentelemetry"}
#: workspace members are installed editable, which packages_distributions() does not map
WORKSPACE = {"agentic": "agentic-core", "sdlc_commons": "sdlc-commons"}


def _declared() -> set[str]:
    project = tomllib.loads((CLI / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = set()
    for requirement in project["dependencies"]:
        name = requirement.split(";")[0].split("[")[0]
        for stop in "<>=!~ ":
            name = name.split(stop)[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


def _imported() -> set[str]:
    found = set()
    for path in (CLI / "src" / "agentic_cli").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found - set(sys.stdlib_module_names) - {"agentic_cli"} - OPTIONAL


@pytest.mark.parametrize("module", sorted(_imported()))
def test_every_imported_distribution_is_declared(module):
    names = [WORKSPACE[module]] if module in WORKSPACE else packages_distributions()[module]
    distributions = {d.lower().replace("_", "-") for d in names}
    assert distributions & _declared(), f"{module} ({distributions}) is not in cli/pyproject"
