"""Rewrite the public eval corpus manifest after a reviewed change (M1-59).

    uv run python scripts/write_eval_manifest.py [CORPUS]

The corpus is validated first (`load_corpus(public=True)` minus the manifest check), so a
manifest is never written over a corpus the harness would refuse; when validation fails,
the old manifest is put back byte for byte.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agentic.eval.corpus import MANIFEST, CorpusError, load_corpus, write_manifest

REPO = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    root = Path(argv[0]) if argv else REPO / "library" / "eval"
    path = root / MANIFEST
    old = path.read_bytes() if path.is_file() else None
    path.unlink(missing_ok=True)  # load_corpus checks a manifest when one is there
    try:
        corpus = load_corpus(root, public=True)
    except CorpusError as error:
        if old is not None:  # the committed manifest stays as it was (DF-389)
            path.write_bytes(old)
        print(error, file=sys.stderr)
        return 1
    manifest = write_manifest(root)
    print(f"{len(corpus.cases)} cases, {len(manifest.files)} files: {manifest.sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
