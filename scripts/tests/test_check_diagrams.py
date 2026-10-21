"""The diagram check finds every Mermaid block of a Markdown file (M1-73). Rendering
needs Docker and is run by hand: `uv run python scripts/check_diagrams.py <file.md>`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_diagrams import IMAGE, diagrams  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def test_every_mermaid_block_is_found_and_other_blocks_are_not(tmp_path: Path):
    doc = tmp_path / "doc.md"
    doc.write_bytes(
        b"# T\r\n\r\n```mermaid\r\nflowchart TD\r\n  a --> b\r\n```\r\n\r\n"
        b"```python\r\nprint(1)\r\n```\r\n\r\n```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n"
    )
    assert diagrams(doc) == ["flowchart TD\n  a --> b\n", "sequenceDiagram\n  A->>B: hi\n"]


def test_the_call_flow_has_its_diagrams_and_the_image_is_pinned():
    assert len(diagrams(ROOT / "docs" / "architecture" / "call-flow.md")) >= 5
    name, _, tag = IMAGE.rpartition(":")
    assert name and tag.count(".") == 2  # an exact release, never `latest`
