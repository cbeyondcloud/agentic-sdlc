"""Exit codes of every `agentic` command (M1-49), so scripts and CI can act on them."""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1  # something unexpected went wrong
    REFUSED = 2  # a safety or precondition check refused; nothing was changed (spec §24)
    INVALID = 3  # a definition, policy or config file is invalid or violates policy
    PAUSED = 4  # the run is waiting for a person at a gate
    STOPPED = 5  # the run stopped at a limit (budget, turns, time, stuck) or was killed
    USAGE = 64  # wrong command, flag or argument (sysexits EX_USAGE)
    INTERRUPTED = 130  # Ctrl-C


EXIT_CODE_TABLE: tuple[tuple[ExitCode, str], ...] = (
    (ExitCode.OK, "Success."),
    (ExitCode.ERROR, "Unexpected error; the message says what failed."),
    (ExitCode.REFUSED, "Refused by a safety or precondition check; nothing was changed."),
    (ExitCode.INVALID, "A definition, policy or config file is invalid or violates policy."),
    (ExitCode.PAUSED, "The run is paused, waiting for a person at a gate."),
    (ExitCode.STOPPED, "The run stopped at a limit (budget, turns, time, stuck) or was killed."),
    (ExitCode.USAGE, "Wrong command, flag or argument; see --help."),
    (ExitCode.INTERRUPTED, "Interrupted (Ctrl-C)."),
)
