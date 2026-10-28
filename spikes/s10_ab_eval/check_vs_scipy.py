"""Cross-check abstats against scipy (throwaway env only).

uv run --no-project --python 3.12 --with scipy==1.18.1 python spikes/s10_ab_eval/check_vs_scipy.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
import abstats  # noqa: E402

rng = random.Random(7)
worst = {"sign": 0.0, "wilcoxon_exact": 0.0, "wilcoxon_normal": 0.0}
for trial in range(300):
    n = rng.choice([8, 12, 20, 30, 40])
    if trial % 2:
        # continuous diffs, no ties -> exact path for n <= 25
        d = [round(rng.gauss(0.4, 1.2), 6) for _ in range(n)]
    else:
        # rubric-like integer diffs with ties and zeros -> normal path
        d = [rng.choice([-2, -1, 0, 0, 1, 1, 2, 3]) for _ in range(n)]
    s = abstats.sign_test(d)
    m = s["pos"] + s["neg"]
    if m:
        ref = stats.binomtest(s["pos"], m, 0.5).pvalue
        worst["sign"] = max(worst["sign"], abs(ref - s["p_two_sided"]))
    w = abstats.wilcoxon_signed_rank(d)
    if w["n"] == 0:
        continue
    if w["method"] == "exact":
        ref = stats.wilcoxon(d, zero_method="wilcox", method="exact").pvalue
        worst["wilcoxon_exact"] = max(worst["wilcoxon_exact"], abs(ref - w["p_two_sided"]))
    else:
        ref = stats.wilcoxon(
            d, zero_method="wilcox", correction=True, method="approx"
        ).pvalue
        worst["wilcoxon_normal"] = max(worst["wilcoxon_normal"], abs(ref - w["p_two_sided"]))

lo, hi = abstats.wilson(7, 10)
print("max |p - scipy| over 300 random cases:", {k: f"{v:.2e}" for k, v in worst.items()})
print(f"Wilson 7/10: [{lo:.3f}, {hi:.3f}]")
