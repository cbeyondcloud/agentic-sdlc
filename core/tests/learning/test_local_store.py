"""Standalone proposals (M3-13): kept in the repository's `.agentic/local/learning/`, updated
in place, and a rejection remembered."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentic.learning.clustering import Proposal
from agentic.learning.store import LocalLearningStore, StoreError

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def proposal(pid="lp-" + "a" * 20, evidence=("fb-1", "fb-2", "fb-3")) -> Proposal:
    return Proposal(
        id=pid,
        agent="developer",
        scope="repo:github.com/acme/app",
        lesson="Wrap outbound HTTP calls in with_retry().",
        evidence=evidence,
        sources=tuple(f"s{n}" for n in range(len(evidence))),
        actors=("alice",),
        created_at=NOW,
    )


def test_saved_proposals_read_back(tmp_path: Path):
    store = LocalLearningStore(tmp_path / "learning")
    assert store.proposals() == []
    store.save([proposal()])
    assert store.proposals() == [proposal()]


def test_saving_again_replaces_by_id(tmp_path: Path):
    store = LocalLearningStore(tmp_path)
    store.save([proposal()])
    grown = proposal(evidence=("fb-1", "fb-2", "fb-3", "fb-4"))
    store.save([grown, proposal("lp-" + "b" * 20)])
    assert [(p.id[:4], len(p.evidence)) for p in store.proposals()] == [
        ("lp-a", 4),
        ("lp-b", 3),
    ]


def test_a_rejection_is_remembered(tmp_path: Path):
    store = LocalLearningStore(tmp_path)
    store.save([proposal()])
    later = NOW + timedelta(days=1)
    rejected = store.reject("lp-" + "a" * 20, later)
    assert (rejected.status, rejected.decided_at) == ("rejected", later)
    assert store.proposals()[0].status == "rejected"
    with pytest.raises(StoreError, match="no proposal"):
        store.reject("lp-" + "c" * 20, later)


def test_a_damaged_file_is_named_not_ignored(tmp_path: Path):
    (tmp_path / "proposals.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError, match="proposals.json"):
        LocalLearningStore(tmp_path).proposals()
