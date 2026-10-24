"""Wiring core to commons for one machine: what each agent step works with (M1-30, DF-46).

The engine and the agent tools (core) know only small protocols; the run's trees
and the guard are Zyroflo-backed (commons). This is the one place that joins them:

  * `trees_for(run_id)` - the run's per-step checkpoints (`StepCheckpoints`);
  * `batch_budget_for(run_id)` - the diff-size budget per review batch over those
                          checkpoints (`ReviewBatchBudget`, M2-24): Zyroflo's budget
                          tightened by org policy's `review_batch`;
  * `tools_for(step)`   - the step's `ToolContext`: the run's tree, a guard built
                          from the step's resolved agent (its allow, ask and deny
                          lists), and a sandbox - `srt` with rules compiled from the
                          guard and org policy, or, when this machine has none and
                          the regime allows it, a plain process (D38); plus the
                          repo's installed skills (M1-38) and, for an agent with the
                          `codeintel` tool, a code-review-graph index of the run's
                          tree kept in run state (DF-87) - or, without one on this
                          machine, `NO_CODEINTEL`: no codeintel tools (DF-226);
  * `scanner_for(step)` - the secret scanner from org policy (`production_scanner`,
                          DF-148), for the backends' `scanner_for` and on every
                          step's `ToolContext.secret_scanner`; without one, the
                          scanner's default (block, audit log) applies.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from agentic.agents.tools import LocalProcessSandbox, SkillCatalog, ToolContext
from agentic.codeintel import Candidates, CodeIntel, CodeIntelError
from agentic.engine.engine import StepContext
from agentic.policy import OrgPolicy, ResolvedAgent, ReviewBatchPolicy, SandboxPolicy
from agentic.sandbox import SandboxRules, SrtSandbox, expand_home
from agentic.sandbox.egress import egress_hosts
from agentic.sandbox.readiness import SandboxDecision
from agentic.security.secret_scan import SecretScanEvent, SecretScanner
from sdlc_commons.guards import Guard
from sdlc_commons.review_batches import ReviewBatchBudget
from sdlc_commons.worktrees import RunWorktrees, StepCheckpoints


class NoCodeIntel:
    """No code intelligence for this run (the codeintel extra is not installed, or the
    repo sets `codeintel.adapter: none`): an adapter that supports no operation, so an
    agent allowed `codeintel` gets no codeintel tools and uses search instead (DF-226)."""

    name = "none"
    supports: frozenset[str] = frozenset()

    def _none(self, *args: object) -> Candidates:
        raise CodeIntelError("no code intelligence on this machine")

    dependents = impact = tests_for = _none

    def overview(self) -> str:
        raise CodeIntelError("no code intelligence on this machine")


NO_CODEINTEL = NoCodeIntel()


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
    batch: ReviewBatchPolicy = field(default_factory=ReviewBatchPolicy)  # org policy (M2-24)
    gateway_url: str | None = None  # TokenLean: the one model host an agent process reaches
    # org policy's `checkpoint_exclude`: added to what a checkpoint leaves out (DF-315)
    checkpoint_exclude: tuple[str, ...] = ()
    _trees: dict[str, StepCheckpoints] = field(default_factory=dict, repr=False)
    _homes: dict[str, Path] = field(default_factory=dict, repr=False)

    def trees_for(self, run_id: str) -> StepCheckpoints:
        if run_id not in self._trees:
            self._trees[run_id] = StepCheckpoints(
                self.target, run_id, exclude=self.checkpoint_exclude
            )
        return self._trees[run_id]

    def batch_budget_for(self, run_id: str) -> ReviewBatchBudget:
        """The run's review batch budget: the guard's (Zyroflo's, which a repository may
        only tighten) tightened again by org policy, with the org's exemptions."""
        guard = Guard(
            self.target,
            run_id,
            max_files=self.batch.max_files,
            max_lines=self.batch.max_lines,
            budget_exempt=self.batch.exempt,
        )
        return ReviewBatchBudget(guard, self.trees_for(run_id))

    def tools_for(self, step: StepContext) -> ToolContext:
        if self.decision.refusal:
            raise ValueError(self.decision.refusal)
        return self._services(self._tools(step), step)

    def _services(self, context: ToolContext, step: StepContext) -> ToolContext:
        codeintel = None
        if "codeintel" in step.agent.tools:
            if self.codeintel_for is None:  # no index on this machine: search instead
                codeintel = NO_CODEINTEL
            else:
                data = self.state_root / step.run_id / "codeintel"
                codeintel = self.codeintel_for(context.tree, data)
        return replace(context, skills=self.skills, codeintel=codeintel)

    def _tools(self, step: StepContext) -> ToolContext:
        tree = self._home(step.run_id)
        guard = Guard.for_agent(self.target, step.run_id, step.agent)
        pass_env = tuple(self.policy.pass_env)
        if not self.decision.use_srt:
            return ToolContext(
                tree=tree,
                guard=guard,
                sandbox=LocalProcessSandbox(pass_env),
                allow_unisolated=True,
                secret_scanner=self.scanner,
            )
        rules = self._rules(guard, step.run_id, tree, egress_hosts(self.policy))
        settings = self.state_root / step.run_id / "sandbox" / step.agent.definition.name
        sandbox = SrtSandbox(rules, settings, pass_env=pass_env)
        return ToolContext(tree=tree, guard=guard, sandbox=sandbox, secret_scanner=self.scanner)

    def process_wrap_for(
        self,
        step: StepContext,
        *,
        write: tuple[Path, ...] = (),
        deny: tuple[Path, ...] = (),
    ) -> list[str]:
        """The srt command prefix for a whole agent CLI process (claude-code, codex,
        opencode; M2-42, DF-443): it reaches the gateway and the registries and no other
        host, every request goes through srt's proxy (`proxy_everything`), it writes only
        the run's tree, policy's caches and `write` (the agent's per-step home), and the
        guard's protected paths and `deny` (the agent's own config inside that home,
        DF-425) stay unwritable - for the agent's own shell commands too, which run inside
        the same sandbox. Refused on a machine without srt."""
        if not self.decision.use_srt:
            raise ValueError(
                self.decision.refusal
                or "an agent CLI process runs only inside the srt sandbox, and this machine "
                "has none ready (agentic init says what to install)"
            )
        if not self.gateway_url:
            raise ValueError("an agent CLI process needs the gateway URL to allow its host")
        tree = self._home(step.run_id)
        guard = Guard.for_agent(self.target, step.run_id, step.agent)
        hosts = egress_hosts(self.policy, self.gateway_url)
        rules = self._rules(guard, step.run_id, tree, hosts, extra_write=write, extra_deny=deny)
        name = f"{step.agent.definition.name}-process"
        settings = self.state_root / step.run_id / "sandbox" / name
        sandbox = SrtSandbox(rules, settings, pass_env=tuple(self.policy.pass_env))
        return sandbox.command_prefix(proxy_everything=True)

    def _rules(
        self,
        guard: Guard,
        run_id: str,
        tree: Path,
        hosts: tuple[str, ...],
        *,
        extra_write: tuple[Path, ...] = (),
        extra_deny: tuple[Path, ...] = (),
    ) -> SandboxRules:
        denials = self._denials(guard, run_id)
        denied = [*(p for d in denials for p in d.paths), *extra_deny]
        return SandboxRules(
            write=(tree, *extra_write, *expand_home(self.policy.allow_write, self.home)),
            deny_write=tuple(dict.fromkeys(denied)),
            deny_read=expand_home(self.policy.effective_deny_read, self.home),
            allowed_domains=hosts,
        )

    def scanner_for(self, step: StepContext) -> SecretScanner:
        """The step's secret scanner (the backends' `scanner_for`)."""
        return self.scanner if self.scanner is not None else SecretScanner()

    def denied_writes(self, step: StepContext, *, also: tuple[str, ...] = ()) -> list[str]:
        """Why what the step changed in the run's trees since its base may not be kept
        (commits the agent made itself included): each changed path the agent's guard
        refuses (Zyroflo's protected paths, the framework floor, the agent's deny list,
        and `also` - more deny globs, such as other agent CLIs' steering files) - matched
        with the guard's own globs, so a deeper `**/x` and any other glob srt cannot
        enforce are caught (DF-443). For an agent CLI, whose writes (its shell commands'
        included) never pass the file tools' check. Names paths only, never content."""
        agent = step.agent
        if also:
            deny = list(dict.fromkeys((*agent.deny_paths, *also)))
            agent = agent.model_copy(update={"deny_paths": deny})
        guard = Guard.for_agent(self.target, step.run_id, agent)
        changed = self.trees_for(step.run_id).changed()
        return list(dict.fromkeys(block.why for block in guard.changes(changed)))

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
