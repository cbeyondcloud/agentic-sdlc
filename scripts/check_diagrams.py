"""Render every Mermaid diagram of Markdown files with a pinned mermaid-cli (M1-73).

    uv run python scripts/check_diagrams.py docs/architecture/call-flow.md [more.md ...]

mermaid-cli needs Node and a headless Chromium, which are native binaries: they run
only inside the project's official Linux container image, never on the host
(Windows laptops may not run unsigned native tools). Docker must be running. Each
diagram is rendered to SVG in a scratch folder; a diagram that does not parse fails
the check with mermaid's message. Not part of `check.py`: the image is large.
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: The mermaid-cli project's own image (MIT), pinned to a release older than two weeks.
IMAGE = "minlag/mermaid-cli:11.4.2"
_BLOCK = re.compile(r"^```mermaid[ \t]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


def diagrams(path: Path) -> list[str]:
    return _BLOCK.findall(path.read_text(encoding="utf-8").replace("\r\n", "\n"))


def render(source: str, work: Path, name: str) -> tuple[bool, str]:
    (work / f"{name}.mmd").write_text(source, encoding="utf-8", newline="\n")
    done = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{work.resolve().as_posix()}:/data",
            IMAGE,
            "-q",
            "-i",
            f"/data/{name}.mmd",
            "-o",
            f"/data/{name}.svg",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    ok = done.returncode == 0 and (work / f"{name}.svg").is_file()
    return ok, (done.stderr or done.stdout).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if shutil.which("docker") is None:
        print("docker is not installed or not on PATH", file=sys.stderr)
        return 2
    failed = 0
    work = Path(tempfile.mkdtemp(prefix="agentic-diagrams-"))
    try:
        for path in args.files:
            found = diagrams(path)
            if not found:
                print(f"{path}: no mermaid diagrams")
            for index, source in enumerate(found, 1):
                ok, message = render(source, work, f"{path.stem}-{index}")
                print(f"{path}: diagram {index}: {'ok' if ok else 'FAILED'}")
                if not ok:
                    failed += 1
                    print(message, file=sys.stderr)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
