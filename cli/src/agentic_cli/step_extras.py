"""What the framework adds at the end of a step's instructions (`StepRunners.prompt_extras`).

  * The review pre-pass (M2-48, DF-228). An agent whose type org policy names in
    `review_prepass.agent_types` (the Code Review agent) gets the deterministic checks over
    the run's change when its step runs. These are the built-in checks (`agentic.review`)
    and the pinned scanners through Zyroflo and the verified tool cache
    (`sdlc_commons.review_prepass`). The full report is kept in the run folder
    (`prepass/<step>-<iteration>.json`). A pre-pass that cannot run says so; it is never
    left out silently.

Everything here is built when the step runs, never when its runner is built (DF-146).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from agentic.engine.engine import StepContext
from agentic.policy import OrgPolicy
from agentic.review.prepass import builtin_checks, merge, prefixed, render
from agentic.security.secret_scan import SecretScanner
from sdlc_commons.review_prepass import ToolLocator, external_checks
from sdlc_commons.tool_fetch import ToolFetcher, ToolFetchError, current_platform
from sdlc_commons.worktrees import WorktreeError, change_since_base

if TYPE_CHECKING:
    from agentic_cli.runtime import Runtime

CANNOT_RUN = (
    "## Pre-pass results\n\nThe pre-pass did not run: {why}. Report that no pre-pass ran - "
    "this is not a clean result - and review the change yourself for secrets, vulnerable "
    "dependencies and licences."
)


def tool_locator(home: Path, policy: OrgPolicy) -> ToolLocator:
    """The scanners `agentic init` installed from the org mirror (verified cache only)."""
    try:
        platform = current_platform()
    except ToolFetchError:
        return ToolLocator(policy.tool_pins, None, "unknown")
    cache = None
    if policy.tool_pins and policy.tool_mirror_url:
        try:
            cache = ToolFetcher(
                home / "tools", policy.tool_mirror_url, download=_never, platform=platform
            )
        except ToolFetchError:
            cache = None
    return ToolLocator(policy.tool_pins, cache, platform)


def _never(url: str) -> bytes:
    raise OSError("the review pre-pass never downloads a tool")


class StepExtras:
    def __init__(
        self,
        target: Path,
        policy: OrgPolicy,
        locator: ToolLocator,
        write_report: Callable[[str, str, str], None],
    ) -> None:
        self.target, self.policy, self.locator = target, policy, locator
        self.write_report = write_report  # (run id, run-relative path, text)

    @classmethod
    def for_runtime(cls, rt: Runtime) -> StepExtras:
        policy = rt.ws.policy()

        def write_report(run_id: str, rel: str, text: str) -> None:
            rt.recorder(run_id).write_artifact(rel, text)

        return cls(rt.ws.repo, policy, tool_locator(rt.ws.home, policy), write_report)

    def __call__(self, step: StepContext) -> str:
        parts = [self.prepass(step)]
        return "\n\n".join(part for part in parts if part)

    # ── the review pre-pass ──────────────────────────────────────────────────

    def prepass(self, step: StepContext) -> str:
        rules = self.policy.review_prepass
        if not rules.enabled or step.agent.type not in rules.agent_types:
            return ""
        try:
            changes = change_since_base(self.target, step.run_id)
        except WorktreeError as error:
            return CANNOT_RUN.format(why=f"the run's trees cannot be read ({error})")
        if not changes:
            return CANNOT_RUN.format(why="the run has no working tree yet")
        scanner = SecretScanner.from_policy(self.policy.secret_scan, on_event=lambda e: None)
        documents = []
        for component, tree, diff_text in changes:
            builtin = builtin_checks(
                diff_text, scanner=scanner, blocked_licences=self.policy.blocked_licences
            )
            external = external_checks(
                tree, diff_text, self.locator, online=rules.online_vulnerability_lookup
            )
            document = merge(builtin, external)
            if len(changes) > 1:  # an estate: name each component's rows and paths
                document = prefixed(document, component)
                for row in document["tools"]:
                    row["name"] = f"{row['name']} ({component})"
            documents.append(document)
        report = merge(*documents)
        rel = f"prepass/{step.step}-{step.iteration}.json"
        self.write_report(step.run_id, rel, json.dumps(report, indent=2, sort_keys=True) + "\n")
        return render(report)


__all__ = ["StepExtras", "tool_locator"]
