"""The diff-size budget per review batch (M2-24, spec §6.1): a Developer step whose
change exceeds the budget is asked to split its work - once automatically, then by a
person at the `diff-budget` gate - and the reviewer only ever sees batches within it.

The run's trees are the FakeTrees; the budget is a fake `ReviewBatch` that reads how
big each checkpoint is from what the developer "wrote" (`sizes`)."""

import asyncio

from _engine_support import FakeTrees, answer, review

from agentic.engine.gates import Approve, Edit, Reject, StandaloneAuthorizer

STARTER, LEAD = "dev@acme.example", "lead@acme.example"
DEV_REVIEW = """\
name: test
version: 1.0.0
steps:
  - id: develop
    agent: dev
  - id: review
    agent: reviewer
"""
LIMITS = "at most 2 files"


class SizedTrees(FakeTrees):
    """FakeTrees whose every checkpoint records how many files the step changed."""

    def __init__(self) -> None:
        super().__init__()
        self.sizes: dict[str, int] = {}
        self.pending_size = 0

    def finish(self, message: str) -> dict[str, str]:
        head = super().finish(message)
        self.sizes[head["app"]], self.pending_size = self.pending_size, 0
        return head


class Budget:
    """A ReviewBatch over SizedTrees: over when a checkpoint changed more than 2 files."""

    def __init__(self, trees: SizedTrees) -> None:
        self.trees, self.checked = trees, []

    def limits(self) -> str:
        return LIMITS

    def over(self, base, head) -> list[str]:
        self.checked.append((dict(base), dict(head)))
        files = self.trees.sizes.get(head["app"], 0)
        return [f"{files} files in one batch, budget 2 - split the work"] if files > 2 else []


def developer(trees: SizedTrees, files: int):
    """A developer answer that changes `files` files in the tree."""

    def write(ctx):
        trees.pending_size = files
        return answer("dev-report", open_questions=[])

    return write


def setup(make_harness, *dev_sizes: int, pipeline: str = DEV_REVIEW):
    trees = SizedTrees()
    budget = Budget(trees)
    script = {
        "dev": [developer(trees, n) for n in dev_sizes],
        "reviewer": [review()],
    }
    h = make_harness(
        pipeline,
        script,
        trees_for=lambda run_id: trees,
        batch_budget_for=lambda run_id: budget,
    )
    return h, trees, budget


def start(h):
    return asyncio.run(h.engine.start("test", {}, STARTER, "r-1"))


def decide(h, decision):
    return asyncio.run(
        h.engine.decide(
            "r-1", decision, authorizer=StandaloneAuthorizer(), self_approval=lambda a, s: None
        )
    )


def test_a_writing_step_is_told_the_budget(make_harness):
    h, _, _ = setup(make_harness, 1)
    assert start(h).status == "completed"
    assert "Review batch budget" in h.runners.prompts["dev"][0]
    assert LIMITS in h.runners.prompts["dev"][0]
    assert "within at most" not in h.runners.prompts["dev"][0]  # DF-480


def test_the_reviewer_sees_only_batches_within_budget(make_harness):  # Review Focus 6
    h, trees, budget = setup(make_harness, 5, 2)
    state = start(h)

    assert state.status == "completed"
    assert h.runners.calls["dev"] == 2  # sent back once, to split its work
    assert "split" in h.runners.prompts["dev"][1].lower()
    assert "5 files" in h.runners.prompts["dev"][1]
    rollbacks = [c for c in trees.calls if c[0] == "rollback"]
    assert rollbacks == [("rollback", {"app": "c1"})]  # the oversized batch is undone
    outcomes = [(r.step, r.outcome) for r in state.history]
    assert outcomes == [
        ("develop", "over-budget"),
        ("develop", "completed"),
        ("review", "completed"),
    ]
    # every batch the reviewer was handed is within budget
    reviewed = [r for r in state.history if r.step == "develop" and r.outcome == "completed"]
    assert all(trees.sizes[r.commit["app"]] <= 2 for r in reviewed)
    finished = [e.get("outcome") for e in h.ledger("r-1").events() if e["event"] == "step_finished"]
    assert finished[:2] == ["over-budget", "completed"]  # the ledger shows the refusal


def test_a_second_breach_waits_for_a_person_and_review_has_not_run(make_harness):
    h, _, _ = setup(make_harness, 5, 4)
    state = start(h)

    assert state.status == "awaiting_gate"
    assert state.pending.id == "diff-budget" and state.pending.step == "develop"
    assert "4 files" in state.pending.reason
    assert "reviewer" not in h.runners.calls  # the reviewer never saw it
    assert list(state.pending.artefacts) == ["handoffs/develop-1-attempt2.md"]


def test_a_person_may_accept_an_oversized_batch(make_harness):
    h, _, _ = setup(make_harness, 5, 4)
    start(h)
    state = decide(h, Approve("diff-budget", LEAD, notes="one generated client"))

    assert state.status == "completed", state.pending
    assert h.runners.calls["reviewer"] == 1
    entry = h.gates["r-1"].entries[-1]
    assert (entry["gate"], entry["verdict"], entry["approver"]) == ("diff-budget", "approve", LEAD)


def test_a_person_may_send_it_back_to_be_split(make_harness):
    h, trees, _ = setup(make_harness, 5, 4, 2)
    start(h)
    state = decide(h, Reject("diff-budget", LEAD, feedback="split by module"))

    assert state.status == "completed"
    assert h.runners.calls["dev"] == 3
    assert "split by module" in h.runners.prompts["dev"][2]
    rollbacks = [c for c in trees.calls if c[0] == "rollback"]
    assert len(rollbacks) == 2  # the split request's, then the rejection's: back to the base


def test_a_step_without_a_change_or_without_trees_is_not_checked(make_harness):
    h = make_harness(
        DEV_REVIEW,
        {"dev": [answer("dev-report", open_questions=[])], "reviewer": [review()]},
        batch_budget_for=lambda run_id: (_ for _ in ()).throw(AssertionError("no trees")),
    )
    assert start(h).status == "completed"


def test_a_budget_that_cannot_be_measured_pauses_the_run(make_harness):
    h, _, budget = setup(make_harness, 1)

    def broken(base, head):
        raise RuntimeError("git diff failed")

    budget.over = broken
    state = start(h)
    assert (state.status, state.pending.id) == ("paused", "worktree")
    assert "git diff failed" in state.pending.reason


def test_an_invalid_oversized_answer_tells_the_person_deciding(make_harness):
    h, trees, _ = setup(make_harness, 5)
    h.runners.script["dev"] = [lambda ctx: setattr(trees, "pending_size", 5) or "not a handoff"]
    state = start(h)
    assert state.pending.id == "invalid-output"
    assert "larger than one review batch" in state.pending.reason


def test_a_blocked_and_oversized_answer_waits_at_the_diff_budget_gate(make_harness):
    """Approving a blocked handoff must not hand the reviewer an oversized batch (F1)."""
    h, trees, _ = setup(make_harness, 5, 5)

    def blocked_and_big(ctx):
        trees.pending_size = 5
        return answer("dev-report", status="blocked", open_questions=[])

    h.runners.script["dev"] = [blocked_and_big, blocked_and_big]
    state = start(h)

    assert (state.status, state.pending.id) == ("awaiting_gate", "diff-budget")
    assert "blocked" in state.pending.reason and "5 files" in state.pending.reason
    assert "reviewer" not in h.runners.calls


def test_an_edited_invalid_oversized_answer_still_waits_at_the_diff_budget_gate(make_harness):
    """Editing an invalid answer into a valid one does not wave its oversized change
    through to the reviewer: a person still decides at the diff-budget gate (F1)."""
    h, trees, _ = setup(make_harness, 5)
    h.runners.script["dev"] = [lambda ctx: setattr(trees, "pending_size", 5) or "not a handoff"]
    state = start(h)
    rel = next(iter(state.pending.artefacts))
    edit = Edit("invalid-output", LEAD, rel, answer("dev-report", open_questions=[]))

    state = decide(h, edit)
    assert (state.status, state.pending.id) == ("awaiting_gate", "diff-budget")
    assert "5 files" in state.pending.reason
    assert "reviewer" not in h.runners.calls
    assert list(state.pending.artefacts) == [f"{rel}.edited-1.md"]

    state = decide(h, Approve("diff-budget", LEAD))
    assert state.status == "completed", state.pending
    assert h.runners.calls["reviewer"] == 1


def test_the_run_time_limit_comes_before_the_split_attempt(make_harness):
    timed = DEV_REVIEW.replace("version: 1.0.0\n", "version: 1.0.0\nmax_minutes: 60\n")
    h, trees, _ = setup(make_harness, 5, 1, pipeline=timed)

    def slow_and_big(ctx):
        h.clock.advance(120)  # the run's limit passes during the oversized attempt
        trees.pending_size = 5
        return answer("dev-report", open_questions=[])

    h.runners.script["dev"] = [slow_and_big, developer(trees, 1)]
    state = start(h)
    assert state.pending.id == "run-timeout"
    assert h.runners.calls["dev"] == 1
