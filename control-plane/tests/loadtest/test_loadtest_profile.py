"""The run profile and the re-chained events of the load harness (M3-22)."""

import json
import random
from collections import Counter
from datetime import UTC, datetime, timedelta

from cp_loadtest.chain import RunChain
from cp_loadtest.profile import Timing, load_template, plan_run

from sdlc_commons.ledger import verify_entries

POLICY = "sha256:" + "ab" * 32  # as long as the recorded run's version
TEAM = "team-03"
RUN_1, RUN_2 = "feature-delivery-20261029-090000-0001", "feature-delivery-20261029-090000-0002"


def test_template_is_a_feature_delivery_run():
    template = load_template()
    kinds = Counter(entry["event"] for entry in template)
    assert len(template) == 40
    assert template[0]["event"] == "run_opened" and template[-1]["event"] == "run_closed"
    assert kinds["step_started"] == kinds["step_finished"] == 14
    assert kinds["gate_waiting"] == kinds["gate_decided"] == 4  # one rejected and asked again
    assert kinds["loop_back"] == 2
    assert verify_entries(template, run_id=template[0]["run_id"]) == []


def _chained(run_id: str) -> list[dict]:
    template = load_template()
    chain = RunChain(template, run_id=run_id, team=TEAM, policy_version=POLICY)
    start = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
    return [chain.next(start + timedelta(seconds=n)) for n in range(len(template))]


def test_chain_verifies_and_keeps_template_shape():
    template = load_template()
    first = _chained(RUN_1)
    assert chain_done(first, template)
    assert verify_entries(first, run_id=RUN_1, identity=(TEAM, POLICY)) == []
    assert [e["event"] for e in first] == [e["event"] for e in template]
    for made, recorded in zip(first, template, strict=True):
        assert set(made) == set(recorded)  # the same fields: realistic sizes
        size, original = len(json.dumps(made)), len(json.dumps(recorded))
        assert abs(size - original) < 0.1 * original
    assert first[0]["owner"] == TEAM
    assert first[1]["at"] == "2026-10-29T09:00:01.000+00:00"
    second = _chained(RUN_2)
    assert verify_entries(second, run_id=RUN_2, identity=(TEAM, POLICY)) == []
    assert {e["hash"] for e in first}.isdisjoint(e["hash"] for e in second)


def chain_done(entries: list[dict], template: list[dict]) -> bool:
    return len(entries) == len(template) and entries[-1]["event"] == "run_closed"


def test_chain_refuses_more_entries_than_the_template():
    template = load_template()[:2]
    chain = RunChain(template, run_id="lt-run-0003", team=TEAM, policy_version=POLICY)
    now = datetime(2026, 10, 29, tzinfo=UTC)
    chain.next(now)
    assert not chain.done
    chain.next(now)
    assert chain.done
    try:
        chain.next(now)
    except IndexError:
        pass
    else:
        raise AssertionError("a finished chain must not grow")


def test_plan_pauses_at_gates_and_follows_timing():
    template = load_template()
    timing = Timing(step_s=60, gate_s=30, heartbeat_s=60, kill_poll_s=10, jitter=0.0)
    actions = plan_run(template, timing, random.Random(7))
    events = [a for a in actions if a.kind == "event"]
    assert [a.index for a in events] == list(range(len(template)))
    kinds = [a.kind for a in actions]
    assert kinds.count("gate") == kinds.count("resume") == 4
    for n, action in enumerate(actions):
        if action.kind == "gate":  # right after the gate_waiting event
            assert template[actions[n - 1].index]["event"] == "gate_waiting"
            assert actions[n + 1].kind == "resume" and actions[n + 1].delay_s == 30
            assert template[actions[n + 2].index]["event"] == "gate_decided"
    finishes = [a for a in events if template[a.index]["event"] == "step_finished"]
    assert all(a.delay_s == 60 for a in finishes)
    total = sum(a.delay_s for a in actions)
    assert 14 * 60 + 4 * 30 <= total < 14 * 60 + 4 * 30 + 30


def test_plan_jitter_stays_in_bounds_and_is_seeded():
    template = load_template()
    timing = Timing(step_s=10, gate_s=10, heartbeat_s=60, kill_poll_s=10, jitter=0.5)
    one = plan_run(template, timing, random.Random(1))
    assert one == plan_run(template, timing, random.Random(1))
    assert one != plan_run(template, timing, random.Random(2))
    long_waits = [a.delay_s for a in one if a.delay_s >= 1]
    assert long_waits and all(5 <= d <= 15 for d in long_waits)
