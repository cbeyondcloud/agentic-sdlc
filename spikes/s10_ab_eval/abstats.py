"""S10 throwaway: the paired statistics the eval harness needs, stdlib only.

The proposal for M1-63 is that `agentic.eval.stats` ships exactly these functions with no
numpy/scipy dependency (the CLI is installed on 1,000 laptops; scipy is ~40 MB for four
functions). `check_vs_scipy.py` cross-checks them against scipy in a throwaway env.

All functions take per-CASE values: repeats of the same case are averaged first, so the
unit of resampling is the case (repeats are correlated, and resampling them as if they were
independent cases overstates the evidence).
"""

from __future__ import annotations

import math
import random
from statistics import fmean


def paired_bootstrap(
    diffs: list[float], *, resamples: int = 10_000, alpha: float = 0.05, seed: int = 0
) -> dict:
    """Percentile bootstrap of the mean per-case difference (B - A).

    Returns the mean, the two-sided (1 - alpha) interval, and the one-sided lower bound at
    level alpha (that is the decision statistic: promote when lower_one_sided > margin).
    The seed is fixed and recorded, so the report is reproducible byte for byte.
    """
    if not diffs:
        raise ValueError("no cases: an empty comparison is unmeasurable, not a tie")
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(fmean(rng.choices(diffs, k=n)) for _ in range(resamples))

    def q(p: float) -> float:
        return means[min(resamples - 1, max(0, int(p * resamples)))]

    return {
        "n": n,
        "mean": fmean(diffs),
        "ci_low": q(alpha / 2),
        "ci_high": q(1 - alpha / 2),
        "lower_one_sided": q(alpha),
        "resamples": resamples,
        "seed": seed,
    }


def signflip_test(
    diffs: list[float], *, margin: float = 0.0, perms: int = 20_000, seed: int = 0
) -> dict:
    """Paired sign-flip randomisation test, one-sided: H1 mean(d) > margin.

    The DECISION statistic proposed for every A/B. Under H0 A and B are exchangeable within
    a case (randomised run order + blinded judging make that true), so flipping the sign of
    each case's difference is equally likely; the p-value is exact up to Monte Carlo error,
    with no normality assumption. Non-inferiority uses a negative margin.
    """
    if not diffs:
        raise ValueError("no cases: an empty comparison is unmeasurable, not a tie")
    rng = random.Random(seed)
    x = [d - margin for d in diffs]
    obs = fmean(x)
    hits = 0
    for _ in range(perms):
        if fmean(v if rng.random() < 0.5 else -v for v in x) >= obs - 1e-12:
            hits += 1
    return {"n": len(x), "margin": margin, "mean_minus_margin": obs,
            "p_one_sided": (hits + 1) / (perms + 1), "perms": perms, "seed": seed}


def sign_test(diffs: list[float]) -> dict:
    """Exact two-sided sign test; zero differences (ties) are dropped and counted."""
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    m = pos + neg
    if m == 0:
        return {"pos": 0, "neg": 0, "ties": len(diffs), "p_two_sided": 1.0}
    k = min(pos, neg)
    tail = sum(math.comb(m, i) for i in range(k + 1)) / 2**m
    return {"pos": pos, "neg": neg, "ties": len(diffs) - m, "p_two_sided": min(1.0, 2 * tail)}


def wilcoxon_signed_rank(diffs: list[float]) -> dict:
    """Wilcoxon signed-rank, zeros dropped (Wilcoxon's method), average ranks for ties.

    Exact null distribution when n <= 25 and there are no tied |d| values; otherwise the
    normal approximation with tie correction and continuity correction. Two-sided p.
    """
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return {"n": 0, "w_plus": 0.0, "p_two_sided": 1.0, "method": "none"}
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    tie_groups = []
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        avg = (i + j) / 2 + 1
        for t in range(i, j + 1):
            ranks[order[t]] = avg
        tie_groups.append(j - i + 1)
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks, d, strict=True) if x > 0)
    has_ties = any(g > 1 for g in tie_groups)
    if n <= 25 and not has_ties:
        # exact: count subsets of {1..n} by rank sum
        max_sum = n * (n + 1) // 2
        counts = [0] * (max_sum + 1)
        counts[0] = 1
        for r in range(1, n + 1):
            for s in range(max_sum, r - 1, -1):
                counts[s] += counts[s - r]
        total = 2**n
        w = min(w_plus, max_sum - w_plus)
        p = 2 * sum(counts[: int(w) + 1]) / total
        return {"n": n, "w_plus": w_plus, "p_two_sided": min(1.0, p), "method": "exact"}
    mean = n * (n + 1) / 4
    var = n * (n + 1) * (2 * n + 1) / 24 - sum(g**3 - g for g in tie_groups) / 48
    z = (abs(w_plus - mean) - 0.5) / math.sqrt(var)
    p = math.erfc(z / math.sqrt(2))
    return {"n": n, "w_plus": w_plus, "p_two_sided": min(1.0, p), "method": "normal"}


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion (used for absolute gates: recall, pass rate)."""
    if n == 0:
        raise ValueError("n = 0: unmeasurable")
    p = successes / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return centre - half, centre + half


def per_case(scores: dict[str, list[float]]) -> dict[str, float]:
    """Average repeats within a case: {case_id: [run scores]} -> {case_id: mean}."""
    return {cid: fmean(v) for cid, v in scores.items() if v}


def paired_diffs(a: dict[str, list[float]], b: dict[str, list[float]]) -> list[float]:
    """Per-case B - A over the cases both variants scored; unscored cases are excluded
    and must be reported beside the denominator by the caller (never silently)."""
    ma, mb = per_case(a), per_case(b)
    return [mb[c] - ma[c] for c in sorted(ma.keys() & mb.keys())]
