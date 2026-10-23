"""scripts/write_eval_manifest.py (M1-59): a corpus that fails validation keeps its
committed manifest byte for byte (DF-389); a valid one gets a fresh manifest."""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from write_eval_manifest import main  # noqa: E402

LIBRARY = Path(__file__).resolve().parents[2] / "library" / "eval"


def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    shutil.copytree(LIBRARY, root)
    return root


def test_a_failing_corpus_keeps_its_manifest(tmp_path: Path):
    root = corpus(tmp_path)
    before = (root / "manifest.json").read_bytes()
    broken = next((root / "cases").rglob("*.yaml"))
    broken.write_text("id: [not closed\n", encoding="utf-8")
    assert main([str(root)]) == 1
    assert (root / "manifest.json").read_bytes() == before


def test_a_valid_corpus_gets_its_manifest(tmp_path: Path):
    root = corpus(tmp_path)
    (root / "manifest.json").unlink()
    assert main([str(root)]) == 0
    assert (root / "manifest.json").read_bytes() == (LIBRARY / "manifest.json").read_bytes()
