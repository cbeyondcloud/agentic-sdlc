"""The shipped agent-quality assets (M1-59): golden handoffs, rubrics, the seed corpus."""

from pathlib import Path

import pytest

from agentic.eval.corpus import build_manifest, floor_shortfalls, load_corpus
from agentic.eval.judge import load_rubric
from agentic.handoffs import HANDOFF_KINDS, parse_handoff

LIBRARY = Path(__file__).resolve().parents[3] / "library" / "eval"


@pytest.mark.parametrize("kind", sorted(HANDOFF_KINDS))
def test_three_golden_handoffs_per_kind_and_every_one_parses(kind: str):
    files = sorted((LIBRARY / "golden" / kind).glob("*.md"))
    assert len(files) >= 3
    for path in files:
        handoff = parse_handoff(path.read_text(encoding="utf-8"))
        assert handoff.envelope.kind == kind, path.name
        assert handoff.envelope.status == "complete", path.name


def test_rubrics_load():
    rubrics = {p.stem: load_rubric(p) for p in (LIBRARY / "rubrics").glob("*.yaml")}
    assert set(rubrics) == {"ba-requirements", "design"}
    assert all(r.max_score == 10 for r in rubrics.values())
    assert rubrics["ba-requirements"].kind == "requirements"


def test_seed_corpus_is_public_current_and_covers_every_class():
    corpus = load_corpus(LIBRARY, public=True)  # decision 17: synthetic or permissive only
    assert build_manifest(LIBRARY).sha == corpus.manifest.sha
    counts = corpus.counts()
    assert set(counts) >= {"scoping", "review", "dev_task", "qa"}
    assert all(row["dev"] >= 1 for row in counts.values())
    rubric_ids = {p.stem for p in (LIBRARY / "rubrics").glob("*.yaml")}
    assert {c.rubric for c in corpus.cases if c.rubric} <= rubric_ids
    for case in corpus.select():
        if case.expected.golden:
            assert parse_handoff(corpus.path(case.expected.golden).read_text(encoding="utf-8"))


def test_the_remaining_decision_18_counts_are_known():
    """The seed corpus is a start, not the gate corpus: what is left is deferred explicitly
    (pilot-repo harvest and seeded-defect tooling)."""
    shortfalls = floor_shortfalls(load_corpus(LIBRARY, public=True))
    assert {s.split(":")[0] for s in shortfalls} <= {"scoping", "review", "dev_task", "qa"}
