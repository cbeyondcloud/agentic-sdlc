"""`agentic policy check [PATH]`: the policy-repo pull request gate (M2-51, DF-367)."""

import json
import shutil
from pathlib import Path

from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "policy-repo"


def test_policy_check_passes_the_template(tmp_path: Path, capsys):
    """The template as day 0 sets it up (README step 1: the library's pipelines copied in,
    which its example overlay changes)."""
    root = tmp_path / "org-policy"
    shutil.copytree(TEMPLATE, root)
    shutil.copytree(ROOT / "library" / "pipelines", root / "pipelines", dirs_exist_ok=True)
    assert main(["--json", "policy", "check", str(root)]) == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert out == {"path": str(root), "problems": []}


def test_policy_check_names_an_overlay_of_a_missing_pipeline(tmp_path: Path, capsys):
    """M2-23: before the library is copied in, the example overlay's pipeline is missing,
    and the gate says so by file (never an overlay silently ignored)."""
    root = tmp_path / "org-policy"
    shutil.copytree(TEMPLATE, root)
    assert main(["--json", "policy", "check", str(root)]) == ExitCode.INVALID
    (problem,) = json.loads(capsys.readouterr().out)["problems"]
    assert problem.startswith("teams/payments/pipelines/overlays/payments-api.yaml: ")
    assert "feature-delivery" in problem


def test_policy_check_names_each_problem_and_exits_3(tmp_path: Path, capsys):
    root = tmp_path / "org-policy"
    shutil.copytree(TEMPLATE, root)
    (root / "policy" / "tiers.yml").write_text("tiers: {}\n", encoding="utf-8")
    assert main(["--json", "policy", "check", str(root)]) == ExitCode.INVALID
    error = json.loads(capsys.readouterr().err)
    assert "1 problem" in error["error"]


def test_policy_check_defaults_to_the_current_folder(tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "org-policy"
    shutil.copytree(TEMPLATE, root)
    shutil.copytree(ROOT / "library" / "pipelines", root / "pipelines", dirs_exist_ok=True)
    monkeypatch.chdir(root)
    assert main(["policy", "check"]) == ExitCode.OK
    assert "passes" in capsys.readouterr().out
