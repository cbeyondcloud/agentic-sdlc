"""The diagram check finds every Mermaid block of a Markdown file (M1-73). Rendering
needs Docker and is run by hand: `uv run python scripts/check_diagrams.py <file.md>`."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_diagrams  # noqa: E402
from check_diagrams import IMAGE, diagrams  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def test_every_mermaid_block_is_found_and_other_blocks_are_not(tmp_path: Path):
    doc = tmp_path / "doc.md"
    doc.write_bytes(
        b"# T\r\n\r\n```mermaid\r\nflowchart TD\r\n  a --> b\r\n```\r\n\r\n"
        b"```python\r\nprint(1)\r\n```\r\n\r\n```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n"
    )
    assert diagrams(doc) == ["flowchart TD\n  a --> b\n", "sequenceDiagram\n  A->>B: hi\n"]


def test_a_diagram_the_renderer_rejects_fails_the_check(tmp_path: Path, monkeypatch, capsys):
    """The failure path, without Docker: a renderer exit 1 (or no SVG) is exit 1 (DF-285 b)."""
    doc = tmp_path / "doc.md"
    doc.write_text("```mermaid\nflowchart TD\n  a -->\n```\n", encoding="utf-8")

    def renderer(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Parse error on line 2")

    monkeypatch.setattr(check_diagrams.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(check_diagrams.subprocess, "run", renderer)
    assert check_diagrams.main([str(doc)]) == 1
    captured = capsys.readouterr()
    assert "diagram 1: FAILED" in captured.out and "Parse error" in captured.err


def test_the_call_flow_has_its_diagrams_and_the_image_is_pinned():
    assert len(diagrams(ROOT / "docs" / "architecture" / "call-flow.md")) >= 5
    name, _, tag = IMAGE.rpartition(":")
    assert name and tag.count(".") == 2  # an exact release, never `latest`
