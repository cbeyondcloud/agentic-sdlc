"""The control plane's load test (M3-22).

    uv run python control-plane/loadtest/loadtest.py up [--replicas 2]
    uv run python control-plane/loadtest/loadtest.py run [--runs 1000 --duration 600 ...]
    uv run python control-plane/loadtest/loadtest.py down

See README.md next to this file."""

import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cp_loadtest.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
