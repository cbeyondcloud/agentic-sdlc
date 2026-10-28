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


def test_the_corpus_meets_the_decision_18_floors():
    """M1-59: the public corpus alone is enough for the M1-64 gate, and the BA and Design
    medians are each taken over 20 holdout cases."""
    corpus = load_corpus(LIBRARY, public=True)
    assert floor_shortfalls(corpus) == []
    for agent in ("business-analyst", "design"):
        assert len(corpus.select(agent=agent, split="holdout")) >= 20, agent
        assert corpus.select(agent=agent, split="dev"), agent


def test_the_corpus_is_varied():
    """Several languages in the review diffs, Windows and POSIX concerns, many domains."""
    corpus = load_corpus(LIBRARY, public=True)
    review_tags = {t for c in corpus.cases if c.case_class == "review" for t in c.tags}
    languages = {"python", "typescript", "go", "java", "csharp", "rust", "sql", "powershell"}
    assert languages | {"bash", "yaml", "kotlin"} <= review_tags
    all_tags = {t for c in corpus.cases for t in c.tags}
    assert {"windows", "macos", "posix", "crlf", "file-locking", "encoding"} <= all_tags
    domains = {t for c in corpus.cases if c.case_class in ("scoping", "qa") for t in c.tags}
    assert len(domains) >= 15


def test_ci_replays_only_the_recorded_fixture_subset():
    """Fixture mode stays fast: cassettes exist for the `fixture` cases and no others."""
    corpus = load_corpus(LIBRARY, public=True)
    recorded = {p.name for p in (LIBRARY / "fixtures" / "cassettes").iterdir()}
    assert recorded == {c.id for c in corpus.select(tag="fixture")}
    assert len(recorded) < len(corpus.cases) / 4
