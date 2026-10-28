"""Repeated corrections become proposals (M3-13, spec §24 "Detect repeats"): similar lessons
cluster per agent and scope; a cluster becomes a proposal at the policy threshold (default 3
in 30 days); a rejected lesson is not proposed again until new evidence reaches it."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agentic.learning.clustering import LearningSettings, Lesson, propose
from agentic.learning.lessons import EmbeddingSimilarity, LexicalSimilarity, alike

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
APP, WEB = "github.com/acme/app", "github.com/acme/web"
SETTINGS = LearningSettings()
RETRY = [
    "Wrap outbound HTTP calls in the with_retry() helper.",
    "Use the with_retry helper around HTTP calls.",
    "HTTP calls should be wrapped with with_retry().",
    "Wrap the HTTP call in with_retry, as everywhere else.",
    "Remember the with_retry() helper for HTTP calls.",
    "with_retry() must wrap every outbound HTTP call.",
]
_n = iter(range(10_000))


def lesson(text: str, *, agent="developer", repo=APP, days_ago=1.0, source=None, actor="alice"):
    n = next(_n)
    return Lesson(
        feedback_id=f"fb-{n:020x}",
        agent=agent,
        text=text,
        at=NOW - timedelta(days=days_ago),
        source=source or f"pr:{repo}#7:comment:{n}",
        actor=actor,
        team="payments",
        repo=repo,
    )


def test_settings_default_to_three_in_thirty_days():
    assert (SETTINGS.threshold, SETTINGS.window_days, SETTINGS.similarity) == (3, 30, 0.5)


def test_two_similar_corrections_make_no_proposal():
    assert propose([lesson(RETRY[0]), lesson(RETRY[1])], SETTINGS, NOW) == []


def test_three_similar_corrections_make_one_proposal():
    lessons = [lesson(RETRY[0], days_ago=3), lesson(RETRY[1], days_ago=2), lesson(RETRY[2])]
    found = propose(lessons, SETTINGS, NOW)
    assert len(found) == 1
    proposal = found[0]
    assert (proposal.agent, proposal.scope, proposal.status) == (
        "developer",
        f"repo:{APP}",
        "pending",
    )
    assert proposal.lesson == RETRY[0]  # the cluster's first lesson
    assert proposal.evidence == tuple(item.feedback_id for item in lessons)
    assert proposal.id.startswith("lp-") and proposal.created_at == NOW
    assert propose(lessons, SETTINGS, NOW)[0].id == proposal.id  # deterministic


def test_corrections_outside_the_window_do_not_count():
    lessons = [lesson(RETRY[0], days_ago=31), lesson(RETRY[1]), lesson(RETRY[2])]
    assert propose(lessons, SETTINGS, NOW) == []
    tighter = replace(SETTINGS, window_days=60)
    assert len(propose(lessons, tighter, NOW)) == 1


def test_same_source_counts_once():
    comment = "pr:github.com/acme/app#7:comment:1"
    lessons = [
        lesson(RETRY[0], agent="code-review", source=comment),  # the pr-review record
        lesson(RETRY[1], agent="code-review", source=comment),  # its review-miss record
        lesson(RETRY[2], agent="code-review"),
    ]
    assert propose(lessons, SETTINGS, NOW) == []


def test_dissimilar_lessons_do_not_cluster():
    lessons = [
        lesson(RETRY[0]),
        lesson("Name database migrations after the table they change."),
        lesson("Log the request id on every error."),
    ]
    assert propose(lessons, SETTINGS, NOW) == []


def test_clusters_are_per_agent_and_repo():
    split_agents = [lesson(RETRY[0]), lesson(RETRY[1]), lesson(RETRY[2], agent="code-review")]
    assert propose(split_agents, SETTINGS, NOW) == []
    two_repos = [lesson(RETRY[0]), lesson(RETRY[1]), lesson(RETRY[2], repo=WEB)]
    found = propose(two_repos, SETTINGS, NOW)
    assert [(p.scope, len(p.evidence)) for p in found] == [("team:payments", 3)]


def test_a_repo_proposal_is_preferred_to_a_team_one():
    lessons = [lesson(t) for t in RETRY[:3]] + [lesson(RETRY[3], repo=WEB)]
    assert [p.scope for p in propose(lessons, SETTINGS, NOW)] == [f"repo:{APP}"]


def test_rejected_lesson_is_not_re_proposed():
    first = [lesson(t, days_ago=10) for t in RETRY[:3]]
    rejected = replace(
        propose(first, SETTINGS, NOW)[0], status="rejected", decided_at=NOW - timedelta(days=5)
    )
    more = first + [lesson(RETRY[3], days_ago=2), lesson(RETRY[4], days_ago=1)]
    assert propose(more, SETTINGS, NOW, existing=[rejected]) == []


def test_rejected_lesson_returns_with_enough_new_evidence():
    first = [lesson(t, days_ago=10) for t in RETRY[:3]]
    rejected = replace(
        propose(first, SETTINGS, NOW)[0], status="rejected", decided_at=NOW - timedelta(days=5)
    )
    more = first + [lesson(t, days_ago=2) for t in RETRY[3:6]]
    found = propose(more, SETTINGS, NOW, existing=[rejected])
    assert len(found) == 1 and found[0].id != rejected.id
    assert set(found[0].evidence) == {item.feedback_id for item in more[3:]}


def test_pending_proposal_is_updated_not_duplicated():
    lessons = [lesson(t, days_ago=3) for t in RETRY[:3]]
    pending = propose(lessons, SETTINGS, NOW)[0]
    assert propose(lessons, SETTINGS, NOW, existing=[pending]) == []  # nothing new
    grown = propose(lessons + [lesson(RETRY[3])], SETTINGS, NOW, existing=[pending])
    assert [(p.id, len(p.evidence), p.created_at) for p in grown] == [
        (pending.id, 4, pending.created_at)
    ]


def test_short_unrelated_lessons_sharing_one_word_do_not_cluster():
    """Lessons that share a single generic word ('add', 'rename', 'use the existing') are
    not the same correction, however short they are."""
    similarity = LexicalSimilarity()
    pairs = [
        ("Add tests", "Add logging"),
        ("Add a docstring", "Add a test"),
        ("Rename the variable", "Rename the endpoint"),
        ("Wrap HTTP calls in with_retry()", "Wrap DB calls in a transaction"),
        ("Use the existing queue", "Use the existing cache"),
    ]
    for a, b in pairs:
        assert not alike(similarity, a, b), (a, b)
    assert alike(similarity, "Retry.", "retry")  # one word each, the same word
    lessons = [lesson("Add tests"), lesson("Add logging"), lesson("Add a docstring")]
    assert propose(lessons, SETTINGS, NOW) == []


def test_older_evidence_arriving_late_grows_the_pending_proposal():
    """A correction made before the proposal's first lesson, delivered later (another
    developer's CLI, a capture-pr of an older comment), joins the pending proposal."""
    lessons = [lesson(t, days_ago=d) for t, d in zip(RETRY[:3], (10, 9, 8), strict=True)]
    pending = propose(lessons, SETTINGS, NOW)[0]
    older = lesson(RETRY[3], days_ago=20)
    grown = propose([older, *lessons], SETTINGS, NOW, existing=[pending])
    assert [(p.id, p.lesson, len(p.evidence)) for p in grown] == [(pending.id, pending.lesson, 4)]


def test_the_seed_leaving_the_window_keeps_the_pending_proposal():
    lessons = [lesson(t, days_ago=d) for t, d in zip(RETRY[:3], (10, 9, 8), strict=True)]
    pending = propose(lessons, SETTINGS, NOW)[0]
    later = NOW + timedelta(days=20.5)  # the 10-day-old seed is now outside the window
    newer = Lesson(**{**vars(lesson(RETRY[3])), "at": later - timedelta(days=1)})
    grown = propose([*lessons, newer], SETTINGS, later, existing=[pending])
    assert [(p.id, len(p.evidence)) for p in grown] == [(pending.id, 4)]


def test_lexical_similarity_is_deterministic():
    similarity = LexicalSimilarity()
    assert similarity.score(RETRY[0], RETRY[1]) >= 0.5
    assert similarity.score(RETRY[0], "Log the request id on every error.") < 0.5
    assert similarity.score("", RETRY[0]) == 0.0


def test_embedding_similarity_is_optional():
    class FakeEmbedder:
        def embed(self, texts):
            words = ("retry", "again", "backoff")
            return [
                [1.0, 0.0] if any(w in text.lower() for w in words) else [0.0, 1.0]
                for text in texts
            ]

    similarity = EmbeddingSimilarity(FakeEmbedder(), threshold=0.9)
    lessons = [
        lesson("Retry transient failures."),
        lesson("Try the request again after a pause."),
        lesson("Use exponential backoff."),
    ]
    assert len(propose(lessons, SETTINGS, NOW, similarity=similarity)) == 1
    assert propose(lessons, SETTINGS, NOW) == []  # lexically these are not alike
