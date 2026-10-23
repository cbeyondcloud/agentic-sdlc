"""Test helper: the scripted `codex exec --json` stand-in (core/tests/backends/fake_codex.py)
behind a `codex --version` that answers the pinned version, as the CLI's install check
and the backend ask before a step (never the real binary)."""

import runpy
import sys
from pathlib import Path

FAKE = Path(__file__).resolve().parents[3] / "core" / "tests" / "backends" / "fake_codex.py"

if __name__ == "__main__":
    if sys.argv[1:] == ["--version"]:
        from agentic.backends.codex import CODEX_VERSION

        print(f"codex-cli {CODEX_VERSION}")
        sys.exit(0)
    sys.argv = [str(FAKE), *sys.argv[1:]]
    runpy.run_path(str(FAKE), run_name="__main__")
