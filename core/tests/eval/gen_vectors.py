"""Generate `stats_vectors.json`: inputs and scipy's answers for `agentic.eval.stats`.

scipy is not a dependency (native wheels; numpy's licence includes Zlib, which is not on
the allow-list), so this runs only in a throwaway environment inside the Linux container:

    uv run --no-project --python 3.12 --with scipy==1.18.1 \
        python core/tests/eval/gen_vectors.py

The inputs are drawn from a fixed seed, so a re-run writes the same file. The rules for
which Wilcoxon method applies mirror `wilcoxon_signed_rank` (exact for n <= 25 with no
ties among |d|), and sign-flip vectors use values whose means are exact in binary, so
ties are ties for both implementations.
"""

import json
import random
from pathlib import Path

import numpy as np
from scipy import stats

OUT = Path(__file__).with_name("stats_vectors.json")
rng = random.Random(20261021)


def diffs_sample(trial: int) -> list[float]:
    n = rng.choice([6, 8, 12, 20, 30, 40])
    if trial % 2:
        return [round(rng.gauss(0.4, 1.2), 6) for _ in range(n)]  # continuous, no ties
    return [float(rng.choice([-2, -1, 0, 0, 1, 1, 2, 3])) for _ in range(n)]  # rubric-like


def wilcoxon_method(d: list[float]) -> str | None:
    nonzero = [x for x in d if x != 0]
    if not nonzero:
        return None
    absolute = [abs(x) for x in nonzero]
    return "exact" if len(nonzero) <= 25 and len(set(absolute)) == len(absolute) else "normal"


vectors: dict[str, list] = {key: [] for key in ("sign_test", "wilcoxon", "wilson")}
vectors.update({key: [] for key in ("signflip_exact", "bh", "pearson", "kappa")})

for trial in range(200):
    d = diffs_sample(trial)
    pos, neg = sum(x > 0 for x in d), sum(x < 0 for x in d)
    if pos + neg:
        p = float(stats.binomtest(pos, pos + neg, 0.5).pvalue)
        vectors["sign_test"].append({"diffs": d, "p": p})
    method = wilcoxon_method(d)
    if method == "exact":
        p = stats.wilcoxon(d, zero_method="wilcox", method="exact").pvalue
    elif method == "normal":
        p = stats.wilcoxon(d, zero_method="wilcox", correction=True, method="approx").pvalue
    if method:
        vectors["wilcoxon"].append({"diffs": d, "method": method, "p": float(p)})

for _ in range(40):
    n = rng.choice([5, 10, 20, 40, 60, 100])
    k = rng.randint(0, n)
    low, high = stats.binomtest(k, n).proportion_ci(confidence_level=0.95, method="wilson")
    vectors["wilson"].append({"k": k, "n": n, "low": float(low), "high": float(high)})

for _ in range(30):
    n = rng.choice([4, 6, 8, 10, 12])
    d = [rng.choice([-2, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3]) for _ in range(n)]
    margin = rng.choice([0.0, -0.5])
    shifted = np.array(d) - margin
    result = stats.permutation_test(
        (shifted,),
        lambda x, axis: np.mean(x, axis=axis),
        permutation_type="samples",
        alternative="greater",
        n_resamples=np.inf,
    )
    vectors["signflip_exact"].append({"diffs": d, "margin": margin, "p": float(result.pvalue)})

for _ in range(20):
    m = rng.choice([3, 8, 16, 30])
    p = [round(rng.random() ** 3, 6) for _ in range(m)]
    adjusted = stats.false_discovery_control(p, method="bh")
    vectors["bh"].append({"p": p, "adjusted": [float(v) for v in adjusted]})

for _ in range(20):
    n = rng.choice([5, 10, 30])
    x = [round(rng.gauss(0, 1), 6) for _ in range(n)]
    y = [round(xi * 0.5 + rng.gauss(0, 1), 6) for xi in x]
    vectors["pearson"].append({"x": x, "y": y, "r": float(stats.pearsonr(x, y).statistic)})


def quadratic_kappa(a: list[float], b: list[float], categories: list[float]) -> float:
    """An independent numpy implementation (scipy has no kappa) to cross-check ours."""
    index = {c: i for i, c in enumerate(categories)}
    k = len(categories)
    observed = np.zeros((k, k))
    for x, y in zip(a, b, strict=True):
        observed[index[x], index[y]] += 1
    expected = np.outer(observed.sum(axis=1), observed.sum(axis=0)) / observed.sum()
    i, j = np.indices((k, k))
    weights = (i - j) ** 2 / (k - 1) ** 2
    return float(1 - (weights * observed).sum() / (weights * expected).sum())


for trial in range(20):
    categories = [i / 2 for i in range(21)] if trial % 2 else list(range(11))
    n = rng.choice([10, 20, 40])
    a = [rng.choice(categories) for _ in range(n)]
    b = [min(categories[-1], max(0, x + rng.choice([-1, 0, 0, 1]))) for x in a]
    vectors["kappa"].append(
        {"a": a, "b": b, "categories": categories, "kappa": quadratic_kappa(a, b, categories)}
    )

OUT.write_bytes((json.dumps(vectors, indent=1) + "\n").encode("utf-8"))
print({key: len(value) for key, value in vectors.items()})
