"""Eval case formats and corpus loading (M1-59, M1-63; spike S10 corpus rules)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic.eval.cases import EvalCase
from agentic.eval.corpus import (
    DECISION_18_FLOORS,
    CorpusError,
    build_manifest,
    floor_shortfalls,
    load_corpus,
    write_manifest,
)

SOURCE = {"repo": "synthetic", "synthetic": True, "licence": "CC0-1.0"}


def scoping(case_id: str = "s1", split: str = "holdout", **extra) -> dict:
    return {
        "id": case_id,
        "agent": "business-analyst",
        "case_class": "scoping",
        "split": split,
        "source": SOURCE,
        "inputs": {"prompt": "Add CSV export to invoices."},
        "expected": {"key_points": ["export runs as a background job"]},
        "rubric": "ba-requirements",
        **extra,
    }


def review(case_id: str = "r1", defects: int = 2) -> dict:
    return {
        "id": case_id,
        "agent": "code-review",
        "case_class": "review",
        "split": "holdout",
        "source": SOURCE,
        "inputs": {"prompt": "Review this change.", "diff": "--- a/x.py\n+++ b/x.py\n"},
        "expected": {
            "seeded_defects": [
                {"file": "x.py", "lines": [10 + i, 12 + i], "category": "logic"}
                for i in range(defects)
            ]
        },
    }


def write_case(root: Path, data: dict, folder: str = "scoping") -> Path:
    from agentic.yamlio import dump_yaml

    path = root / "cases" / folder / f"{data['id']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_yaml(data).encode("utf-8"))
    return path


@pytest.mark.parametrize(
    "data, problem",
    [
        (scoping(expected={"key_points": []}), "key_points or a golden"),
        (scoping(rubric=None), "rubric"),
        ({**review(), "expected": {"seeded_defects": []}}, "seeded defect"),
        (
            {**review(), "case_class": "dev_task", "agent": "developer", "expected": {}},
            "tests",
        ),
        ({**review(), "case_class": "qa", "agent": "qa", "expected": {}}, "failing criterion"),
    ],
)
def test_each_class_requires_its_truth(data, problem):
    with pytest.raises(ValidationError, match=problem):
        EvalCase.model_validate(data)


def test_a_seeded_defect_range_must_be_ordered():
    bad = review()
    bad["expected"]["seeded_defects"][0]["lines"] = [12, 10]
    with pytest.raises(ValidationError, match="start"):
        EvalCase.model_validate(bad)


def test_load_select_and_count(tmp_path: Path):
    write_case(tmp_path, scoping("s1"))
    write_case(tmp_path, scoping("s2", split="dev"))
    write_case(tmp_path, review("r1", defects=3), folder="review")
    corpus = load_corpus(tmp_path)
    assert [c.id for c in corpus.select(agent="business-analyst")] == ["s1", "s2"]
    assert [c.id for c in corpus.select(split="holdout")] == ["r1", "s1"]
    counts = corpus.counts()
    assert counts["scoping"] == {"cases": 2, "holdout": 1, "dev": 1, "truth": 0}
    assert counts["review"]["truth"] == 3  # seeded defects


def test_duplicate_ids_refused(tmp_path: Path):
    write_case(tmp_path, scoping("same"))
    write_case(tmp_path, {**review("same")}, folder="review")
    with pytest.raises(CorpusError, match="duplicate case id 'same'"):
        load_corpus(tmp_path)


def test_a_reference_must_stay_inside_the_corpus(tmp_path: Path):
    write_case(tmp_path, scoping(expected={"golden": "../outside.md"}))
    with pytest.raises(CorpusError, match="inside the corpus"):
        load_corpus(tmp_path)


def test_a_missing_golden_file_is_refused(tmp_path: Path):
    write_case(tmp_path, scoping(expected={"golden": "golden/none.md"}))
    with pytest.raises(CorpusError, match="does not exist"):
        load_corpus(tmp_path)


def test_public_corpus_holds_only_synthetic_or_permissive_cases(tmp_path: Path):
    internal = {"repo": "github.com/acme/api", "sha": "a" * 40, "synthetic": False}
    write_case(tmp_path, scoping("gpl", source={**internal, "licence": "GPL-3.0-only"}))
    load_corpus(tmp_path)  # a policy-repo corpus may hold anything its owner allows
    with pytest.raises(CorpusError, match="decision 17"):
        load_corpus(tmp_path, public=True)
    write_case(tmp_path, scoping("gpl", source={**internal, "licence": "Apache-2.0"}))
    assert load_corpus(tmp_path, public=True).cases[0].id == "gpl"


def test_manifest_detects_edits_but_not_line_endings(tmp_path: Path):
    path = write_case(tmp_path, scoping("s1"))
    write_manifest(tmp_path)
    first = load_corpus(tmp_path).manifest.sha
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))  # a Windows checkout
    assert load_corpus(tmp_path).manifest.sha == first
    path.write_bytes(path.read_bytes().replace(b"CSV", b"PDF"))
    with pytest.raises(CorpusError, match="manifest.json is out of date"):
        load_corpus(tmp_path)
    assert build_manifest(tmp_path).sha != first


def test_floor_shortfalls_name_each_class(tmp_path: Path):
    write_case(tmp_path, scoping("s1"))
    write_case(tmp_path, review("r1", defects=2), folder="review")
    shortfalls = floor_shortfalls(load_corpus(tmp_path))
    assert any(s.startswith("scoping: 1 holdout cases") for s in shortfalls)
    assert any("review: 2 seeded defects" in s for s in shortfalls)
    assert DECISION_18_FLOORS["review"]["truth"] == 40
    assert not floor_shortfalls(load_corpus(tmp_path), floors={})
