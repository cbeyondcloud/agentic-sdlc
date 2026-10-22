"""The shell policy compiled to Codex execpolicy rules (M2-40, spike S3 decision 2).

Codex reads Starlark `prefix_rule(...)` calls from `CODEX_HOME/rules/*.rules`:

  * deny  -> `forbidden`, with a justification Codex quotes back to the model;
  * ask   -> `prompt`: under `approval_policy = "never"` a prompt is refused, as the
             claude-code backend refuses ask-listed commands (S3 run S1);
  * allow -> `allow`, but ONLY when srt is the boundary: an allow rule runs the command
             outside Codex's own sandbox (S3 runs Q1/R2), so without srt it would widen
             what the command may touch.

What execpolicy cannot do, whatever we write (S3 Q6): an unlisted command still runs
(no default-deny), and a script with a redirect or substitution is not split, so a
listed command inside it is not matched. The capability matrix reports that gap to
policy (`shell_default_deny`); the PreToolUse hook guard closes it (DF-116).

Rules are word prefixes, as policy writes them; a word may hold alternatives
(`git push|fetch`), which become a pattern alternative list. Every rule carries one
`match` example (the first alternatives), because Codex validates examples at load.
"""

import json
from dataclasses import dataclass

from agentic.policy import ResolvedAgent

HEADER = "# Compiled by agentic from the agent's shell policy. Do not edit: rewritten per step.\n"


@dataclass(frozen=True)
class ShellRules:
    """Word-prefix shell rules: allowed, asked about each time, and denied."""

    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @classmethod
    def of(cls, agent: ResolvedAgent) -> "ShellRules":
        """The resolved agent's own rules (org + team + agent denies, D31)."""
        return cls(tuple(agent.shell), tuple(agent.ask_shell), tuple(agent.deny_shell))


def compile_rules(rules: ShellRules, *, srt_boundary: bool) -> str:
    """The `.rules` file text for these rules; allow rules only under srt."""
    layers = [("forbidden", rules.deny), ("prompt", rules.ask)]
    if srt_boundary:
        layers.append(("allow", rules.allow))
    out, seen = [HEADER], set()
    for decision, prefixes in layers:
        for prefix in prefixes:
            words = prefix.split()
            if not words or prefix in seen:
                continue
            seen.add(prefix)
            out.append(_rule(words, decision, prefix))
    return "".join(out)


def _rule(words: list[str], decision: str, prefix: str) -> str:
    pattern = [_word(word) for word in words]
    example = [word.split("|")[0] for word in words]
    lines = [
        "prefix_rule(",
        f"    pattern = [{', '.join(pattern)}],",
        f"    decision = {_string(decision)},",
    ]
    if decision == "forbidden":
        why = f"agentic shell policy: {prefix!r} is denied (org, team or agent policy)"
        lines.append(f"    justification = {_string(why)},")
    lines.append(f"    match = [[{', '.join(_string(word) for word in example)}]],")
    lines.append(")")
    return "\n".join(lines) + "\n"


def _word(word: str) -> str:
    options = [option for option in word.split("|") if option]
    if len(options) == 1:
        return _string(options[0])
    return "[" + ", ".join(_string(option) for option in options) + "]"


def _string(text: str) -> str:
    """A Starlark string literal (JSON's escapes are a subset of Starlark's)."""
    return json.dumps(text, ensure_ascii=False)
