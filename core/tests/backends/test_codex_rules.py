"""Tests for compiling the shell policy into Codex execpolicy rules (M2-40, spike S3):
deny -> forbidden, ask -> prompt (refused under approval_policy=never), allow -> allow
only when srt is the boundary (an allow rule runs outside Codex's own sandbox)."""

import ast
import re

from agentic.backends.codex_rules import ShellRules, compile_rules


def rules_in(text: str) -> list[dict]:
    """Each `prefix_rule(...)` call as a dict of its keyword arguments (Starlark here is
    a subset of Python call syntax, so the Python parser reads it)."""
    found = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "prefix_rule":
            found.append({kw.arg: ast.literal_eval(kw.value) for kw in node.keywords})
    return found


def test_deny_becomes_forbidden_with_justification():
    [rule] = rules_in(compile_rules(ShellRules(deny=("git push",)), srt_boundary=True))
    assert rule["pattern"] == ["git", "push"]
    assert rule["decision"] == "forbidden"
    assert "git push" in rule["justification"] and "denied" in rule["justification"]


def test_ask_becomes_prompt():
    [rule] = rules_in(compile_rules(ShellRules(ask=("npm publish",)), srt_boundary=True))
    assert (rule["pattern"], rule["decision"]) == (["npm", "publish"], "prompt")


def test_allow_rules_only_under_srt():  # Review Focus 2: allow escapes Codex's sandbox
    rules = ShellRules(allow=("uv run pytest",), deny=("git push",))
    under_srt = rules_in(compile_rules(rules, srt_boundary=True))
    assert [r["decision"] for r in under_srt] == ["forbidden", "allow"]
    without = rules_in(compile_rules(rules, srt_boundary=False))
    assert [r["decision"] for r in without] == ["forbidden"]


def test_alternatives_become_pattern_lists():
    [rule] = rules_in(compile_rules(ShellRules(deny=("git push|fetch",)), srt_boundary=True))
    assert rule["pattern"] == ["git", ["push", "fetch"]]


def test_every_rule_has_a_match_example():
    rules = ShellRules(allow=("git status|diff",), ask=("npm publish",), deny=("rm -rf",))
    for rule in rules_in(compile_rules(rules, srt_boundary=True)):
        [example] = rule["match"]
        first = [w if isinstance(w, str) else w[0] for w in rule["pattern"]]
        assert example == first


def test_quotes_in_rules_are_escaped():
    text = compile_rules(ShellRules(deny=('echo "x\\y"',)), srt_boundary=True)
    [rule] = rules_in(text)
    assert rule["pattern"] == ["echo", '"x\\y"']


def test_strictest_layer_first_and_each_prefix_once():
    rules = ShellRules(allow=("git", "npm publish"), ask=("npm publish",), deny=("git push",) * 2)
    found = rules_in(compile_rules(rules, srt_boundary=True))
    assert [(r["pattern"], r["decision"]) for r in found] == [
        (["git", "push"], "forbidden"),
        (["npm", "publish"], "prompt"),  # not also allowed: the stricter layer wins
        (["git"], "allow"),
    ]


def test_empty_rules_compile_to_a_header_only():
    text = compile_rules(ShellRules(), srt_boundary=True)
    assert rules_in(text) == []
    assert re.search(r"^#", text, re.M)


def test_of_reads_a_resolved_agent(resolved):
    agent = resolved.model_copy(update={"ask_shell": ["npm publish"]})
    rules = ShellRules.of(agent)
    assert rules.allow == tuple(agent.shell)
    assert rules.deny == tuple(agent.deny_shell)
    assert rules.ask == ("npm publish",)
