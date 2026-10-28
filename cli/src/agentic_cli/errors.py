"""The one error a command raises to stop with a message, a hint and an exit code."""

from agentic_cli.exit_codes import ExitCode


class CliError(Exception):
    def __init__(self, message: str, code: ExitCode = ExitCode.ERROR, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint  # what the user can do about it, shown under the message
