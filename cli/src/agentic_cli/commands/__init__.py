"""The CLI's command groups. Each module exposes `register(app)`; a new command group is
one module plus one line in COMMANDS, so work on different commands rarely collides."""

from collections.abc import Callable

import typer

from agentic_cli.commands import (
    audit,
    basics,
    catalog,
    cost,
    doctor,
    evals,
    gates,
    gc,
    index,
    init,
    killswitch,
    learn,
    login,
    pr,
    rbac,
    runs,
    skills,
    sync,
)

COMMANDS: list[Callable[[typer.Typer], None]] = [
    basics.register,
    init.register,
    login.register,
    catalog.register,
    skills.register,  # after catalog: it joins catalog's `skills` group
    runs.register,
    gates.register,
    index.register,
    cost.register,
    evals.register,
    pr.register,
    sync.register,
    killswitch.register,
    rbac.register,
    audit.register,
    gc.register,
    doctor.register,
    learn.register,
]
