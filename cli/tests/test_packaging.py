"""The CLI's package metadata declares every distribution its code imports, so an
installed CLI never works only by accident of another package's dependencies (DF-206)."""

import ast
import shutil
import subprocess
import sys
import tomllib
import zipfile
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

from agentic_cli.commands.training import CASSETTES, KEY, STEPS

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


@pytest.mark.skipif(shutil.which("uv") is None, reason="building the wheel needs uv")
def test_the_built_wheel_holds_the_training_recording(tmp_path):
    """DF-997: `agentic training-gateway` reads its cassette and steps.json from the
    installed package, so a wheel without them breaks the walkthrough on every laptop."""
    built = subprocess.run(
        ["uv", "build", "--wheel", "--package", "agentic-cli", "--out-dir", str(tmp_path)],
        cwd=CLI.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert built.returncode == 0, built.stderr[-4000:]
    (wheel,) = tmp_path.glob("agentic_cli-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    package = CASSETTES.parents[1]  # the agentic_cli folder
    recorded = {p.relative_to(package.parent).as_posix() for p in CASSETTES.rglob("*")}
    recorded = {name for name in recorded if (package.parent / name).is_file()}
    assert STEPS.relative_to(package.parent).as_posix() in recorded
    assert f"agentic_cli/training/cassettes/{KEY}.json" in recorded
    assert recorded <= names, sorted(recorded - names)
