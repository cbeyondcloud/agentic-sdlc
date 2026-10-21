"""The CLI's command groups. Each module exposes `register(app)`; a new command group is
one module plus one line in COMMANDS, so work on different commands rarely collides."""

from collections.abc import Callable

import typer

from agentic_cli.commands import basics, catalog, cost, evals, gates, init, login, runs

COMMANDS: list[Callable[[typer.Typer], None]] = [
    basics.register,
    init.register,
    login.register,
    catalog.register,
    runs.register,
    gates.register,
    cost.register,
    evals.register,
]
