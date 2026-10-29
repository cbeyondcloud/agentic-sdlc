"""The load harness (M3-22) lives in `control-plane/loadtest/`, a folder of tools, not an
installed package: its tests import `cp_loadtest` from there."""

import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "loadtest"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))
