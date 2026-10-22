"""The shared ACP backend (M2-57, spike S7): one runner for every agent CLI that speaks the
Agent Client Protocol (v1), and one profile per agent, held as data. OpenCode is the
first profile; others (E2-06) are new `AcpProfile` values with their contract tests.

The `acp` SDK (the `acp` extra of agentic-core) is imported only when a run starts."""

from agentic.backends.acp.opencode import OPENCODE
from agentic.backends.acp.profile import AcpProfile, Launch, ShellRules, StepSetup

#: Backend name -> its ACP profile.
PROFILES: dict[str, AcpProfile] = {OPENCODE.name: OPENCODE}

__all__ = ["PROFILES", "AcpProfile", "Launch", "ShellRules", "StepSetup"]
