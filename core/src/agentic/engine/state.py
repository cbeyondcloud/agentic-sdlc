"""The state of one pipeline run: `run.json` in the run folder (M1-21, spec §8.3).

The engine is a state machine; this file is its memory. Each step is a
checkpoint: a step is recorded only after its handoff is written and pinned, and
`run.json` is saved before the next step starts, so a crash re-runs at most the
step that was in progress.

`run.json` is written atomically - a temporary file in the same folder, flushed
to disk, then `os.replace` - so a process killed mid-write (laptop sleep, Ctrl-C)
leaves the previous state whole. It lives next to Zyroflo's `events.jsonl`,
`approvals.jsonl` and `state.json` and never uses their names.
"""

import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

RUN_FILE = "run.json"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

RunStatus = Literal["running", "awaiting_gate", "paused", "completed", "failed", "stopped"]


class RunStateError(Exception):
    """A run's state cannot be read or written; names the file and the reason."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cursor(_Strict):
    """Where the run is: a top-level step, and inside a loop the iteration and body step."""

    index: int = Field(ge=0)
    loop_iteration: int = Field(default=0, ge=0)  # 1-based once inside a loop
    loop_index: int = Field(default=0, ge=0)


class StepRecord(_Strict):
    step: str
    iteration: int = Field(ge=0)
    outcome: Literal["completed", "skipped", "paused", "invalid", "stopped", "failed"]
    handoff: str | None = None  # relative to the run folder
    digest: str | None = None
    base: dict[str, str] | None = None  # the commit each component started from (M1-30)
    commit: dict[str, str] | None = None  # its checkpoint after the step; None = none made
    cost_usd: str = "0"  # Decimal, as text
    started_at: AwareDatetime
    ended_at: AwareDatetime
    detail: str = ""


class Pending(_Strict):
    """What the run waits on: a gate, a pause for a person, or a tool approval."""

    kind: Literal["gate", "pause", "approval"]
    id: str
    reason: str
    artefacts: dict[str, str] = {}  # run-relative path -> digest, pinned by a gate
    since: AwareDatetime
    step: str | None = None
    state_path: str | None = None  # a paused tool approval: the runner's saved state
    state_digest: str | None = None
    calls: list[str] = []  # tool calls awaiting a person's decision


class RunState(_Strict):
    schema_version: int = 1
    run_id: str
    pipeline: str
    pipeline_version: str
    pipeline_digest: str
    policy_version: str
    catalog_digest: str  # the merged handoff templates the run was started with (D37)
    sandbox: str | None = None  # where its commands ran: "srt 0.0.78" or "none" (D38)
    regime: Literal["solo", "team", "regulated"]
    starter: str
    status: RunStatus
    cursor: Cursor
    history: list[StepRecord] = []
    pending: Pending | None = None
    latest: dict[str, str] = {}  # kind -> run-relative path of its newest handoff
    feedback: dict[str, str] = {}  # step -> a person's feedback for its next attempt
    budget: dict[str, Any] = {}  # BudgetLedger.snapshot()
    inputs: dict[str, str] = {}
    started_at: AwareDatetime
    updated_at: AwareDatetime
    max_minutes: int | None = Field(default=None, ge=1)
    extra_minutes: int = Field(default=0, ge=0)  # granted by a person on resume
    unpinned: dict[str, str] = {}  # artefacts written since the last gate: path -> digest
    approved: dict[str, dict[str, str]] = {}  # gate -> what its latest approval pinned
    digests: dict[str, str] = {}  # every handoff the run wrote: path -> digest when recorded
    waited_seconds: float = Field(default=0, ge=0)  # time spent waiting for a person
    loop_pass_start: int = Field(default=0, ge=0)  # history index where this loop pass began
    ended_by: str | None = None  # who stopped the run
    end_reason: str | None = None


class RunStore:
    """Reads and writes `run.json` under the runs root (`.agentic/local/runs`)."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = Path(runs_root)

    def path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or ".." in run_id:
            raise RunStateError(
                f"{run_id!r} is not a run id (letters, digits, '.', '_' and '-'; "
                "no path separators)"
            )
        return self.runs_root / run_id / RUN_FILE

    def exists(self, run_id: str) -> bool:
        return self.path(run_id).is_file()

    def save(self, state: RunState) -> None:
        """Write `run.json` atomically: the old file stays whole if this is interrupted."""
        path = self.path(state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (state.model_dump_json(indent=2) + "\n").encode("utf-8")
        temporary = path.with_name(f"{RUN_FILE}.{os.getpid()}.tmp")
        try:
            with open(temporary, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        _sync_folder(path.parent)

    def load(self, run_id: str) -> RunState:
        path = self.path(run_id)
        if not path.is_file():
            raise RunStateError(f"no run {run_id}: {path} not found")
        try:
            return RunState.model_validate_json(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValidationError) as error:
            raise RunStateError(f"{path}: cannot be read ({error})") from error


def _sync_folder(folder: Path) -> None:
    """Make the rename itself durable (POSIX; Windows has no directory handles to sync)."""
    if os.name == "nt":
        return
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "RUN_FILE",
    "Cursor",
    "Pending",
    "RunState",
    "RunStateError",
    "RunStatus",
    "RunStore",
    "StepRecord",
]
