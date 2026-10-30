"""Trace capture (M3-06, OR-01, D14): prompts and answers stay out of spans unless the team
opts in (`teams/<team>.yaml trace_capture: true`) and the repo's classification allows it
(`classifications.<class>.trace_capture: true`)."""

import json
import shutil
from pathlib import Path

import pytest
from test_call_spans import run_once

from agentic.definitions import load_agent
from agentic.policy import ResolveContext, load_policy, resolve_agent


def resolved_with(fixtures: Path, tmp_path: Path, *, team_opt_in: bool, class_allows: bool):
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    if team_opt_in:
        team = root / "teams" / "payments.yaml"
        team.write_text(
            team.read_text(encoding="utf-8") + "trace_capture: true\n", encoding="utf-8"
        )
    if class_allows:
        classes = root / "classifications.yaml"
        text = classes.read_text(encoding="utf-8").replace(
            "  internal:\n", "  internal:\n    trace_capture: true\n"
        )
        classes.write_text(text, encoding="utf-8")
    agent, violations = resolve_agent(
        load_agent(fixtures / "agents" / "developer.md"),
        load_policy(root),
        ResolveContext(team="payments"),
    )
    assert agent is not None, violations
    return agent


@pytest.mark.parametrize(
    ("team_opt_in", "class_allows", "captured"),
    [(False, False, False), (True, False, False), (False, True, False), (True, True, True)],
)
def test_capture_needs_the_team_and_the_classification(
    fixtures, tmp_path, team_opt_in, class_allows, captured
):
    agent = resolved_with(fixtures, tmp_path, team_opt_in=team_opt_in, class_allows=class_allows)
    assert agent.trace_capture is captured


def _prompt_in_spans(spans) -> bool:
    texts = [json.dumps(dict(span.attributes), default=str) for span in spans.get_finished_spans()]
    return any("Be brief." in text for text in texts)


def test_spans_carry_no_prompt_by_default(fixtures, tmp_path, identity, tokenlean, spans):
    agent = resolved_with(fixtures, tmp_path, team_opt_in=True, class_allows=False)
    run_once(agent, identity, tokenlean, tmp_path)
    assert not _prompt_in_spans(spans)


def test_an_opted_in_team_gets_prompts_in_its_spans(fixtures, tmp_path, identity, tokenlean, spans):
    agent = resolved_with(fixtures, tmp_path, team_opt_in=True, class_allows=True)
    run_once(agent, identity, tokenlean, tmp_path)
    assert _prompt_in_spans(spans)
