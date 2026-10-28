"""The diff-size budget per review batch (M2-24, spec §6.1), over a step's checkpoints.

A Developer step's change is the batch a reviewer reads. `ReviewBatchBudget` measures it
- `git diff base head` of every tree of the run (`StepCheckpoints.diff`) - against
Zyroflo's budget through the guard (`Guard.diff`: changed files and lines, minus the
exempt paths), and words the budget for the agent's prompt. The engine sees it through
its `ReviewBatch` protocol (agentic.engine.engine).
"""

from collections.abc import Mapping

from sdlc_commons.guards import Guard
from sdlc_commons.worktrees import StepCheckpoints


class ReviewBatchBudget:
    """One run's review batch budget: the guard's limits over the run's trees."""

    def __init__(self, guard: Guard, checkpoints: StepCheckpoints) -> None:
        self.guard, self.checkpoints = guard, checkpoints

    def limits(self) -> str:
        """The budget in words, e.g. "at most 40 files and at most 1500 changed lines
        (not counted: **/uv.lock, ...)"."""
        files = self.guard.policy["max_files_per_batch"]
        lines = self.guard.policy["max_lines_per_batch"]
        parts = [f"at most {files} files"] if files else []
        parts += [f"at most {lines} changed lines"] if lines else []
        text = " and ".join(parts) or "no limit"
        if exempt := self.guard.policy["budget_exempt"]:
            text += f" (not counted: {', '.join(exempt)})"
        return text

    def over(self, base: Mapping[str, str], head: Mapping[str, str]) -> list[str]:
        """Why the change from `base` to `head` exceeds the budget; empty when it fits."""
        diff = self.checkpoints.diff(base, head)
        return [block.why for block in self.guard.diff(diff)] if diff else []
