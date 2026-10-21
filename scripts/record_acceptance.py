"""Record the M1 acceptance cassettes (cli/tests/acceptance/cassettes/).

    uv run python scripts/record_acceptance.py [pytest arguments]

No model is called. Each scenario runs through the real CLI with the cassette server in
record mode, forwarding every model call to the scenario's scripted upstream (the
hand-written turns in its test file); a cassette is written only when every call of the
scenario succeeded and every scripted turn was used. Run it after changing a scenario's
script, an agent's prompt or the handoff templates, then run the suite (replay) and
review the cassette diff before committing.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ACCEPTANCE = ROOT / "cli" / "tests" / "acceptance"


def main(argv: list[str]) -> int:
    os.environ["AGENTIC_ACCEPTANCE_RECORD"] = "1"
    return int(pytest.main([str(ACCEPTANCE), "-q", *argv]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
