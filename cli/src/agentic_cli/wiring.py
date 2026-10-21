"""Wiring core to commons for one machine: what each agent step works with (M1-30, DF-46).

The engine and the agent tools (core) know only small protocols; the run's trees
and the guard are Zyroflo-backed (commons). This is the one place that joins them:

  * `trees_for(run_id)` - the run's per-step checkpoints (`StepCheckpoints`);
  * `tools_for(step)`   - the step's `ToolContext`: the run's tree, a guard built
                          from the step's resolved agent (its allow, ask and deny
                          lists), and a sandbox - `srt` with rules compiled from the
                          guard and org policy, or, when this machine has none and
                          the regime allows it, a plain process (D38); plus the
                          repo's installed skills (M1-38) and, for an agent with the
                          `codeintel` tool, a code-review-graph index of the run's
                          tree kept in run state (DF-87);
  * `scanner_for(step)` - the secret scanner from org policy (`production_scanner`,
                          DF-148), for the backends' `scanner_for` and on every
                          step's `ToolContext.secret_scanner`; without one, the
                          scanner's default (block, audit log) applies.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from agentic.agents.tools import LocalProcessSandbox, SkillCatalog, ToolContext
from agentic.codeintel import CodeIntel
from agentic.engine.engine import StepContext
from agentic.policy import OrgPolicy, ResolvedAgent, SandboxPolicy
from agentic.sandbox import SandboxRules, SrtSandbox, expand_home
from agentic.sandbox.readiness import SandboxDecision
from agentic.security.secret_scan import SecretScanEvent, SecretScanner
from sdlc_commons.guards import Guard
from sdlc_commons.worktrees import RunWorktrees, StepCheckpoints


def production_scanner(
    policy: OrgPolicy, on_event: Callable[[SecretScanEvent], None] | None = None
) -> SecretScanner:
    """The scanner a production run uses: org policy's action, detectors and allowed
    values; `on_event` is the run's audit sink (M1-54 passes the run ledger's)."""
    return SecretScanner.from_policy(policy.secret_scan, on_event=on_event)


@dataclass
class StepWiring:
    target: Path  # the repository (or estate) the run works on
    policy: SandboxPolicy
    decision: SandboxDecision
    state_root: Path  # where per-run sandbox settings are kept
    home: Path = field(default_factory=Path.home)
    scanner: SecretScanner | None = None  # production_scanner(org policy), DF-148
    skills: SkillCatalog | None = None  # the repo's installed skills
    codeintel_for: Callable[[Path, Path], CodeIntel] | None = None  # (tree, data folder)
    _trees: dict[str, StepCheckpoints] = field(default_factory=dict, repr=False)
    _homes: dict[str, Path] = field(default_factory=dict, repr=False)

    def trees_for(self, run_id: str) -> StepCheckpoints:
        if run_id not in self._trees:
            self._trees[run_id] = StepCheckpoints(self.target, run_id)
        return self._trees[run_id]

    def tools_for(self, step: StepContext) -> ToolContext:
        if self.decision.refusal:
            raise ValueError(self.decision.refusal)
        return self._services(self._tools(step), step)

    def _services(self, context: ToolContext, step: StepContext) -> ToolContext:
        codeintel = None
        if self.codeintel_for is not None and "codeintel" in step.agent.tools:
            data = self.state_root / step.run_id / "codeintel"
            codeintel = self.codeintel_for(context.tree, data)
        return replace(context, skills=self.skills, codeintel=codeintel)

    def _tools(self, step: StepContext) -> ToolContext:
        tree = self._home(step.run_id)
        guard = Guard.for_agent(self.target, step.run_id, step.agent)
        if not self.decision.use_srt:
            return ToolContext(
                tree=tree,
                guard=guard,
                sandbox=LocalProcessSandbox(),
                allow_unisolated=True,
                secret_scanner=self.scanner,
            )
        denials = self._denials(guard, step.run_id)
        rules = SandboxRules(
            write=(tree, *expand_home(self.policy.allow_write, self.home)),
            deny_write=tuple(dict.fromkeys(p for d in denials for p in d.paths)),
            deny_read=expand_home(self.policy.effective_deny_read, self.home),
            allowed_domains=tuple(self.policy.allowed_domains),
        )
        settings = self.state_root / step.run_id / "sandbox" / step.agent.definition.name
        sandbox = SrtSandbox(rules, settings)
        return ToolContext(tree=tree, guard=guard, sandbox=sandbox, secret_scanner=self.scanner)

    def scanner_for(self, step: StepContext) -> SecretScanner:
        """The step's secret scanner (the backends' `scanner_for`)."""
        return self.scanner if self.scanner is not None else SecretScanner()

    def unenforced(self, agent: ResolvedAgent, run_id: str) -> list[str]:
        """The deny globs `srt` cannot enforce for this agent (the file tools still do),
        for the CLI to show before a run (M1-54)."""
        guard = Guard.for_agent(self.target, run_id, agent)
        found = (u for d in self._denials(guard, run_id) for u in d.unenforced)
        return list(dict.fromkeys(found))

    def unenforced_preview(self, agent: ResolvedAgent) -> list[str]:
        """The agent's own deny globs `srt` could not enforce, worked out on the
        repository itself: nothing is created (for `run --dry-run`, DF-64). The
        framework's protected globs are left out (DF-71: shown every time, they are
        noise)."""
        denials = Guard.for_agent(self.target, "dry-run", agent).write_denials(self.target)
        own = set(agent.deny_paths)
        return [glob for glob in denials.unenforced if glob in own]

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
