"""Compare two export dirs ignoring the id-dependent ordering of edge extra.ambiguous_targets."""

import json
import sys
from pathlib import Path


def norm(line: str) -> str:
    rec = json.loads(line)
    try:
        ex = json.loads(rec.get("extra", "{}"))
        if isinstance(ex.get("ambiguous_targets"), list):
            ex["ambiguous_targets"] = sorted(ex["ambiguous_targets"])
            rec["extra"] = json.dumps(ex, sort_keys=True)
    except (ValueError, AttributeError):
        pass
    return json.dumps(rec, sort_keys=True)


a, b = Path(sys.argv[1]), Path(sys.argv[2])
fa = {str(p.relative_to(a)) for p in a.rglob("*.jsonl")}
fb = {str(p.relative_to(b)) for p in b.rglob("*.jsonl")}
raw_diff = norm_diff = 0
for f in sorted(fa | fb):
    if f not in fa or f not in fb:
        print("only one side:", f)
        norm_diff += 1
        continue
    ta, tb = (a / f).read_text(encoding="utf-8"), (b / f).read_text(encoding="utf-8")
    if ta != tb:
        raw_diff += 1
        if sorted(map(norm, ta.splitlines())) != sorted(map(norm, tb.splitlines())):
            norm_diff += 1
            print("real diff:", f)
print(f"shards={len(fa | fb)} raw_differing={raw_diff} differing_after_normalising_ambiguous_target_order={norm_diff}")
