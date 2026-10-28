"""The paired statistics every eval decision rests on (spike S10), in stdlib Python.

The unit of analysis is the CASE: repeats of one case are correlated, so they are
averaged first (`per_case`) and every interval resamples cases, never runs. Treating
repeats as independent cases would overstate the evidence.

  * `signflip_test`     - DECIDES an A/B: paired sign-flip randomisation test of
                          mean(d - margin) > 0, one-sided. It holds its size at the sample
                          sizes we can afford (S10 measured 3.6-4.5 % false wins where the
                          percentile bootstrap gave 6-8 %). Exact when 2^n <= perms.
  * `paired_bootstrap`  - REPORTED: percentile bootstrap CI of the mean difference.
  * `sign_test`, `wilcoxon_signed_rank` - reported beside them.
  * `wilson`            - interval for an absolute gate's proportion.
  * `cluster_bootstrap_ratio` - a pooled rate (defects caught / seeded) resampled by case.
  * `weighted_kappa`    - quadratic-weighted Cohen's kappa, for judge calibration.
  * `bh_adjusted`, `benjamini_hochberg` - false discovery control over a batch of skills.
  * `pearson`           - the verbosity check (score vs output length).

Every random procedure takes a seed and reports it, so a report reproduces exactly. The
deterministic tests agree with scipy 1.18.1 to 1e-12 (`core/tests/eval/stats_vectors.json`);
scipy itself is not a dependency.
"""

import itertools
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from statistics import fmean


class Unmeasurable(ValueError):
    """Nothing to measure: an empty comparison is a failure, never a tie."""


@dataclass(frozen=True)
class SignFlip:
    n: int
    margin: float
    mean_minus_margin: float
    p_one_sided: float
    perms: int
    seed: int
    exact: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Bootstrap:
    n: int
    mean: float
    ci_low: float
    ci_high: float
    lower_one_sided: float
    resamples: int
    seed: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SignTest:
    pos: int
    neg: int
    ties: int
    p_two_sided: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Wilcoxon:
    n: int
    w_plus: float
    p_two_sided: float
    method: str  # exact, normal, or none (every difference was zero)

    def as_dict(self) -> dict:
        return asdict(self)


def per_case(scores: Mapping[str, Sequence[float | None]]) -> dict[str, float]:
    """Average the repeats within each case, skipping unmeasured runs (None). A case with
    no measured run has no value at all - it is unscored, not zero."""
    out = {}
    for case_id, values in scores.items():
        measured = [v for v in values if v is not None]
        if measured:
            out[case_id] = fmean(measured)
    return out


def paired_diffs(
    a: Mapping[str, Sequence[float | None]], b: Mapping[str, Sequence[float | None]]
) -> tuple[list[float], list[str]]:
    """Per-case B - A over the cases both sides scored (in case-id order), and the ids of
    every other case: the caller reports them beside the denominator, never silently."""
    mean_a, mean_b = per_case(a), per_case(b)
    both = sorted(mean_a.keys() & mean_b.keys())
    unscored = sorted((set(a) | set(b)) - set(both))
    return [mean_b[c] - mean_a[c] for c in both], unscored


def signflip_test(
    diffs: Sequence[float], *, margin: float = 0.0, perms: int = 20_000, seed: int = 0
) -> SignFlip:
    """One-sided paired sign-flip test of H1: mean(d) > margin (a negative margin tests
    non-inferiority). Under H0 the two variants are exchangeable within a case (runs are
    interleaved and judged blind), so every sign pattern is equally likely."""
    if not diffs:
        raise Unmeasurable("no cases: an empty comparison is unmeasurable, not a tie")
    x = [d - margin for d in diffs]
    n = len(x)
    observed = fmean(x)
    tolerance = 1e-9 * max(1.0, abs(observed))
    if 2**n <= perms:
        hits = sum(
            fmean(v * s for v, s in zip(x, signs, strict=True)) >= observed - tolerance
            for signs in itertools.product((1, -1), repeat=n)
        )
        p, exact = hits / 2**n, True
    else:
        rng = random.Random(seed)
        hits = sum(
            fmean(v if rng.random() < 0.5 else -v for v in x) >= observed - tolerance
            for _ in range(perms)
        )
        p, exact = (hits + 1) / (perms + 1), False
    return SignFlip(n, margin, observed, p, perms, seed, exact)


def paired_bootstrap(
    diffs: Sequence[float], *, resamples: int = 10_000, alpha: float = 0.05, seed: int = 0
) -> Bootstrap:
    """Percentile bootstrap of the mean per-case difference: the two-sided (1 - alpha)
    interval and the one-sided lower bound at alpha. Reported, never the decision."""
    if not diffs:
        raise Unmeasurable("no cases: an empty comparison is unmeasurable, not a tie")
    rng = random.Random(seed)
    values = list(diffs)
    means = sorted(fmean(rng.choices(values, k=len(values))) for _ in range(resamples))
    return Bootstrap(
        n=len(values),
        mean=fmean(values),
        ci_low=_quantile(means, alpha / 2),
        ci_high=_quantile(means, 1 - alpha / 2),
        lower_one_sided=_quantile(means, alpha),
        resamples=resamples,
        seed=seed,
    )


def cluster_bootstrap_ratio(
    pairs: Sequence[tuple[float, float]],
    *,
    resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Bootstrap:
    """A pooled rate - sum(numerators) / sum(denominators), e.g. defects caught over
    defects seeded - with its percentile interval from resampling whole cases."""
    if not pairs or sum(den for _, den in pairs) <= 0:
        raise Unmeasurable("no cases with a denominator: the rate is unmeasurable")
    rng = random.Random(seed)
    values = list(pairs)
    ratios = []
    for _ in range(resamples):
        sample = rng.choices(values, k=len(values))
        den = sum(d for _, d in sample)
        if den > 0:
            ratios.append(sum(num for num, _ in sample) / den)
    if not ratios:
        raise Unmeasurable("no resample had a denominator: the rate is unmeasurable")
    ratios.sort()
    return Bootstrap(
        n=len(values),
        mean=sum(num for num, _ in values) / sum(den for _, den in values),
        ci_low=_quantile(ratios, alpha / 2),
        ci_high=_quantile(ratios, 1 - alpha / 2),
        lower_one_sided=_quantile(ratios, alpha),
        resamples=resamples,
        seed=seed,
    )


def sign_test(diffs: Sequence[float]) -> SignTest:
    """Exact two-sided sign test; zero differences are dropped and counted as ties."""
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    m = pos + neg
    if m == 0:
        return SignTest(0, 0, len(diffs), 1.0)
    tail = sum(math.comb(m, i) for i in range(min(pos, neg) + 1)) / 2**m
    return SignTest(pos, neg, len(diffs) - m, min(1.0, 2 * tail))


def wilcoxon_signed_rank(diffs: Sequence[float]) -> Wilcoxon:
    """Wilcoxon signed-rank, two-sided; zeros dropped, average ranks for tied |d|. The
    exact null distribution when n <= 25 with no ties, otherwise the normal approximation
    with tie and continuity corrections (scipy's `method="exact"` / `"approx"`)."""
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return Wilcoxon(0, 0.0, 1.0, "none")
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    tie_groups = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        for t in range(i, j + 1):
            ranks[order[t]] = (i + j) / 2 + 1
        tie_groups.append(j - i + 1)
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks, d, strict=True) if x > 0)
    if n <= 25 and all(g == 1 for g in tie_groups):
        max_sum = n * (n + 1) // 2
        counts = [0] * (max_sum + 1)
        counts[0] = 1
        for r in range(1, n + 1):
            for s in range(max_sum, r - 1, -1):
                counts[s] += counts[s - r]
        w = min(w_plus, max_sum - w_plus)
        p = 2 * sum(counts[: int(w) + 1]) / 2**n
        return Wilcoxon(n, w_plus, min(1.0, p), "exact")
    mean = n * (n + 1) / 4
    var = n * (n + 1) * (2 * n + 1) / 24 - sum(g**3 - g for g in tie_groups) / 48
    z = (abs(w_plus - mean) - 0.5) / math.sqrt(var)
    return Wilcoxon(n, w_plus, min(1.0, math.erfc(z / math.sqrt(2))), "normal")


def wilson(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score interval (95 % by default) for an absolute gate's proportion."""
    if n <= 0:
        raise Unmeasurable("n = 0: the proportion is unmeasurable")
    p = successes / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return centre - half, centre + half


def weighted_kappa(a: Sequence[float], b: Sequence[float], *, categories: Iterable[float]) -> float:
    """Quadratic-weighted Cohen's kappa between two raters over ordered `categories`
    (e.g. rubric totals 0..10, or 0, 0.5, ..., 10 for a mean of two humans)."""
    if len(a) != len(b):
        raise ValueError("both raters must score the same items")
    if not a:
        raise Unmeasurable("no items: kappa is unmeasurable")
    levels = list(categories)
    index = {value: i for i, value in enumerate(levels)}
    if unknown := sorted({v for v in (*a, *b) if v not in index}):
        raise ValueError(f"ratings outside the categories: {unknown}")
    k = len(levels)
    observed = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b, strict=True):
        observed[index[x]][index[y]] += 1
    n = len(a)
    rows = [sum(row) for row in observed]
    cols = [sum(observed[i][j] for i in range(k)) for j in range(k)]
    scale = (k - 1) ** 2 or 1
    disagree = expected = 0.0
    for i in range(k):
        for j in range(k):
            weight = (i - j) ** 2 / scale
            disagree += weight * observed[i][j]
            expected += weight * rows[i] * cols[j] / n
    if expected == 0:
        raise Unmeasurable("every rating is the same category: kappa is undefined")
    return 1 - disagree / expected


def bh_adjusted(pvalues: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values, in the input order (scipy
    `false_discovery_control(method="bh")`)."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        adjusted[i] = running
    return adjusted


def benjamini_hochberg(pvalues: Sequence[float], q: float = 0.10) -> list[bool]:
    """Which hypotheses of a batch are rejected at false discovery rate `q` (S10: each
    default skill is tested on its own, then BH at q = 0.10 across the batch)."""
    return [p <= q for p in bh_adjusted(pvalues)]


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Pearson's r, or None with fewer than three points or no variance."""
    if len(x) != len(y):
        raise ValueError("x and y must be the same length")
    if len(x) < 3:
        return None
    mx, my = fmean(x), fmean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _quantile(sorted_values: Sequence[float], p: float) -> float:
    count = len(sorted_values)
    return sorted_values[min(count - 1, max(0, int(p * count)))]
