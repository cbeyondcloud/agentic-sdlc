"""`agentic skills eval` through the M1-63 harness (M2-46): the agent runs with and without
a personal skill on corpus cases, blind as X/Y, and the report says what changed.

No live model is ever called (owner decision 73): runs replay the library's recorded
fixture cassettes, or record through a fake upstream that answers from them."""

import json
import shutil
from pathlib import Path

import pytest

from agentic.eval.replay import UpstreamReply, UpstreamRequest
from agentic.skills.personal import load_personal, skill_block
from agentic_cli import evaluation

LIBRARY = Path(__file__).resolve().parents[2] / "library" / "eval"
CASSETTES = LIBRARY / "fixtures" / "cassettes"
MARKER = "Always name the rollback plan for every requirement."


@pytest.fixture(autouse=True)
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)  # the eval's ledger goes under ./.agentic/local/


@pytest.fixture
def variant(tmp_path: Path) -> evaluation.SkillVariant:
    folder = tmp_path / "home" / "skills" / "rollback-first"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: rollback-first\ndescription: Use when writing requirements.\n---\n\n"
        f"{MARKER}\n",
        encoding="utf-8",
    )
    [skill] = load_personal(tmp_path / "home").skills
    return evaluation.SkillVariant(skill.name, skill.sha256, skill_block([skill]))


def options(tmp_path: Path, variant, **extra) -> evaluation.RunOptions:
    values = {
        "agent": "business-analyst",
        "corpus": LIBRARY,
        "split": "all",
        "tag": "fixture",
        "mode": "replay",
        "repeat": 1,
        "out": tmp_path / "out",
        "skill": variant,
    }
    return evaluation.RunOptions(**{**values, **extra})


def both_labels(tmp_path: Path) -> Path:
    """The fixture cassettes, with each case's X recording also served for Y."""
    root = tmp_path / "cassettes"
    shutil.copytree(CASSETTES, root)
    for case in root.iterdir():
        if (case / "X").is_dir():
            shutil.copytree(case / "X", case / "Y")
    return root


def test_skill_eval_runs_both_variants_and_writes_the_comparison(tmp_path: Path, variant):
    result = evaluation.execute_run(options(tmp_path, variant, cassettes=both_labels(tmp_path)))
    assert evaluation.failure(result) is None, result.report.problems
    report = result.report
    assert sorted(report.labels.values()) == ["with", "without"]
    assert set(report.aggregates) == {"X", "Y"}
    assert len(report.runs) == 2 * len(report.cases)
    summary = evaluation.write_skill_eval(result, variant)
    saved = json.loads((result.folder / "skill-eval.json").read_text(encoding="utf-8"))
    assert saved == summary
    assert saved["skill"] == "rollback-first" and saved["sha256"] == variant.sha256
    assert saved["verdict"] == "MEASURED" and saved["agent"] == "business-analyst"
    rubric = saved["comparison"]["metrics"]["rubric"]
    assert set(rubric) == {"without", "with", "delta"}
    assert set(saved["comparison"]["cost"]) == {"without", "with"}
    markdown = evaluation.skill_eval_markdown(saved)
    assert "rollback-first" in markdown and "| rubric |" in markdown


def test_with_runs_carry_the_skill_text(tmp_path: Path, variant):
    """Recorded through a fake upstream that answers from the fixture cassettes: the
    agent's requests under the `with` label hold the skill, the `without` ones do not."""
    bodies: dict[str, list[str]] = {}
    served: dict[str, int] = {}

    def upstream(request: UpstreamRequest) -> UpstreamReply:
        case, label, repeat, who = request.key.split("/")
        bodies.setdefault(f"{label}/{who}", []).append(request.body.decode("utf-8"))
        source = CASSETTES / case / "X" / repeat / f"{who}.json"
        calls = json.loads(source.read_bytes())["calls"]
        index = served.get(request.key, 0)
        served[request.key] = index + 1
        call = calls[index]
        body = call["body"].encode("utf-8")
        return UpstreamReply(call["status"], body, call["content_type"], call["headers"])

    run = options(tmp_path, variant, mode="record", cassettes=tmp_path / "recorded")
    result = evaluation.execute_run(run, upstream=upstream)
    assert evaluation.failure(result) is None, result.report.problems
    label_of = {variant: label for label, variant in result.report.labels.items()}
    with_bodies = bodies[f"{label_of['with']}/agent"]
    without_bodies = bodies[f"{label_of['without']}/agent"]
    assert with_bodies and all(MARKER in body for body in with_bodies)
    assert without_bodies and not any(MARKER in body for body in without_bodies)
    # the judge never sees the skill: it scores the output alone
    assert not any(MARKER in b for b in bodies.get(f"{label_of['with']}/judge", []))


def test_without_a_skill_the_run_is_unchanged(tmp_path: Path):
    result = evaluation.execute_run(options(tmp_path, None))
    assert set(result.report.aggregates) == {"X"}
