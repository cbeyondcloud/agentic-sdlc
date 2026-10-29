"""Exit codes of every `agentic` command (M1-49), so scripts and CI can act on them."""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1  # something went wrong, or a check finished with a "no" (init not ready, CI red)
    REFUSED = 2  # a safety or precondition check refused; nothing was changed (spec §24)
    INVALID = 3  # a definition, policy or config file is invalid or violates policy
    PAUSED = 4  # the run is waiting for a person (a gate, an approval, a step error pause)
    STOPPED = 5  # the run stopped at a limit (budget, turns, time, stuck) or was killed
    USAGE = 64  # wrong command, flag or argument (sysexits EX_USAGE)
    INTERRUPTED = 130  # Ctrl-C, SIGTERM or SIGHUP


EXIT_CODE_TABLE: tuple[tuple[ExitCode, str], ...] = (
    (ExitCode.OK, "Success."),
    (
        ExitCode.ERROR,
        "An error; the message says what failed. Also: `init` finished but the machine is "
        "not ready (the report names what to fix), `doctor` found a check that fails, and "
        "`pr checks` read CI and it is not green; with --json, the result (ready, healthy, "
        "state) tells these from a failure.",
    ),
    (ExitCode.REFUSED, "Refused by a safety or precondition check; nothing was changed."),
    (
        ExitCode.INVALID,
        "A definition, policy or config file is invalid or violates policy. Also: "
        "`audit verify` found a run ledger, the org audit chain or an export that does not "
        "verify (each problem is located).",
    ),
    (
        ExitCode.PAUSED,
        "The run is paused, waiting for a person: at a gate, for a tool-call approval, or "
        "after a step error or policy pause (resume or stop it); with --json, "
        "pending.kind and pending.id say which.",
    ),
    (ExitCode.STOPPED, "The run stopped at a limit (budget, turns, time, stuck) or was killed."),
    (ExitCode.USAGE, "Wrong command, flag or argument; see --help."),
    (
        ExitCode.INTERRUPTED,
        "Interrupted: Ctrl-C, or the process was asked to stop (SIGTERM, SIGHUP, a closed "
        "terminal); an interrupted run continues with `agentic resume <run>`.",
    ),
)
