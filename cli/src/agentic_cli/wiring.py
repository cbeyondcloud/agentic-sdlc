"""Wiring core to commons for one machine: what each agent step works with (M1-30, DF-46).

The engine and the agent tools (core) know only small protocols; the run's trees
and the guard are Zyroflo-backed (commons). This is the one place that joins them:

  * `trees_for(run_id)` - the run's per-step checkpoints (`StepCheckpoints`);
  * `tools_for(step)`   - the step's `ToolContext`: the run's tree, a guard built
                          from the step's resolved agent (its allow, ask and deny
                          lists), and a sandbox - `srt` with rules compiled from the
                          guard and org policy, or, when this machine has none and
                          the regime allows it, a plain process (D38).
"""

from dataclasses import dataclass, field
from pathlib import Path

from agentic.agents.tools import LocalProcessSandbox, ToolContext
from agentic.engine.engine import StepContext
from agentic.policy import ResolvedAgent, SandboxPolicy
from agentic.sandbox import SandboxRules, SrtSandbox, expand_home
from agentic.sandbox.readiness import SandboxDecision
from sdlc_commons.guards import Guard
from sdlc_commons.worktrees import RunWorktrees, StepCheckpoints


@dataclass
class StepWiring:
    target: Path  # the repository (or estate) the run works on
    policy: SandboxPolicy
    decision: SandboxDecision
    state_root: Path  # where per-run sandbox settings are kept
    home: Path = field(default_factory=Path.home)
    _trees: dict[str, StepCheckpoints] = field(default_factory=dict, repr=False)
    _homes: dict[str, Path] = field(default_factory=dict, repr=False)

    def trees_for(self, run_id: str) -> StepCheckpoints:
        if run_id not in self._trees:
            self._trees[run_id] = StepCheckpoints(self.target, run_id)
        return self._trees[run_id]

    def tools_for(self, step: StepContext) -> ToolContext:
        if self.decision.refusal:
            raise ValueError(self.decision.refusal)
        tree = self._home(step.run_id)
        guard = Guard.for_agent(self.target, step.run_id, step.agent)
        if not self.decision.use_srt:
            return ToolContext(
                tree=tree, guard=guard, sandbox=LocalProcessSandbox(), allow_unisolated=True
            )
        denials = self._denials(guard, step.run_id)
        rules = SandboxRules(
            write=(tree, *expand_home(self.policy.allow_write, self.home)),
            deny_write=tuple(dict.fromkeys(p for d in denials for p in d.paths)),
            deny_read=expand_home(self.policy.effective_deny_read, self.home),
            allowed_domains=tuple(self.policy.allowed_domains),
        )
        settings = self.state_root / step.run_id / "sandbox" / step.agent.definition.name
        return ToolContext(tree=tree, guard=guard, sandbox=SrtSandbox(rules, settings))

    def unenforced(self, agent: ResolvedAgent, run_id: str) -> list[str]:
        """The deny globs `srt` cannot enforce for this agent (the file tools still do),
        for the CLI to show before a run (M1-54)."""
        guard = Guard.for_agent(self.target, run_id, agent)
        found = (u for d in self._denials(guard, run_id) for u in d.unenforced)
        return list(dict.fromkeys(found))

    def _home(self, run_id: str) -> Path:
        """Where the run's agents work (made once per run, then reused)."""
        if run_id not in self._homes:
            self._homes[run_id] = self.trees_for(run_id).prepare()
        return self._homes[run_id]

    def _denials(self, guard: Guard, run_id: str):
        """Write denials per component tree: deny globs are relative to a repository."""
        self._home(run_id)
        trees = RunWorktrees(self.target, run_id).trees().values()
        return [guard.write_denials(t.path) for t in trees if t.mode == "worktree"]
