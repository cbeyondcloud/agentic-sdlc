"""Thin adapters over Zyroflo (sdlc-harness): ledger, guards, worktrees, lifecycle (D34).

Importing this package points Zyroflo at the framework's run-folder root
(`.agentic/local/runs`, git-ignored) for the whole process (Zyroflo Z3), so every
adapter - and every Zyroflo command this process starts - uses the same runs.
"""

from zyroflo.sdlc import state as _zyroflo_state

from sdlc_commons.lifecycle import RUNS_ROOT

_zyroflo_state.set_runs_root(RUNS_ROOT)
