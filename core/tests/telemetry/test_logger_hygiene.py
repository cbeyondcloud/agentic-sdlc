"""The `agentic` logger never falls back to Python's last-resort stderr handler (DF-175)."""

import logging
import subprocess
import sys


def test_the_agentic_logger_has_a_null_handler():
    import agentic  # noqa: F401

    handlers = logging.getLogger("agentic").handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)


def test_an_unconfigured_process_prints_nothing_for_an_agentic_warning():
    code = (
        "import logging, agentic.engine.engine; "
        "logging.getLogger('agentic.engine').warning('step ended')"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "step ended" not in done.stderr
