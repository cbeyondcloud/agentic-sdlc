"""A huge AGENTS.md is read only up to what is shown (DF-177): the cap applies to the
read, not after reading the whole file into memory."""

from pathlib import Path

from agentic.memory import agents_md
from agentic.memory.agents_md import agents_md_knowledge


def test_only_the_shown_part_is_read(tmp_path: Path, monkeypatch):
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"x" * 5_000_000)
    read: list[int] = []
    real_open = Path.open

    def spy(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if self == path:
            real_read = handle.read

            def counted(size=-1):
                data = real_read(size)
                read.append(len(data))
                return data

            handle.read = counted
        return handle

    monkeypatch.setattr(Path, "open", spy)
    text = agents_md_knowledge(path, tmp_path, limit=1_000)
    assert sum(read) <= 1_000 * agents_md.MAX_BYTES_PER_CHAR + 1
    assert "truncated" in text


def test_a_multibyte_file_is_cut_by_characters(tmp_path: Path):
    path = tmp_path / "AGENTS.md"
    path.write_text("é" * 3_000, encoding="utf-8")
    text = agents_md_knowledge(path, tmp_path, limit=1_000)
    assert "é" * 1_000 in text and "é" * 1_001 not in text
    assert "truncated" in text


def test_a_small_file_is_shown_whole(tmp_path: Path):
    path = tmp_path / "AGENTS.md"
    path.write_text("Use the retry helper.\n", encoding="utf-8")
    text = agents_md_knowledge(path, tmp_path)
    assert "Use the retry helper." in text and "truncated" not in text
