"""S10 throwaway: power of the proposed A/B decision rule, by Monte Carlo.

uv run --no-project --python 3.12 --with numpy==2.5.3 python spikes/s10_ab_eval/power.py

Decision rule simulated (the one the record proposes): average the k repeats of each case
per variant, take per-case differences d_i = B - A, paired percentile bootstrap of mean(d),
"B better" when the one-sided 95 % lower bound > margin (0 for superiority, a negative
margin for non-inferiority). Power = share of simulated experiments that conclude "better"
(or "not worse") when the stated true effect exists. Alpha is checked with effect = 0.

The noise parameters are ASSUMPTIONS (marked in the record); M1-63's first real run
measures them and this script is re-run with the measured values.
"""

from __future__ import annotations

import math

import numpy as np

SIMS = 2000
BOOT = 2000
rng = np.random.default_rng(20261020)


def decide(d: np.ndarray, margin: float) -> np.ndarray:
    """d: (SIMS, n) per-case diffs -> bool (SIMS,) 'lower one-sided 95 % bound > margin'."""
    sims, n = d.shape
    idx = rng.integers(0, n, size=(BOOT, n))
    lows = np.empty(sims)
    for s in range(sims):
        means = d[s][idx].mean(axis=1)
        lows[s] = np.quantile(means, 0.05)
    return lows > margin


# --- rubric (0-10 integer judge scores) -------------------------------------------------
# case quality ~ N(6, 1.5); per-case effect ~ N(delta, 0.5); run-to-run noise sd 0.8
# (agent nondeterminism at production temperature); judge noise sd 0.7 per judgement.
def rubric_diffs(n: int, k: int, delta: float, run_sd=0.8, judge_sd=0.7, het_sd=0.5):
    q = rng.normal(6.0, 1.5, size=(SIMS, n, 1))
    eff = rng.normal(delta, het_sd, size=(SIMS, n, 1))

    def score(shift):
        x = q + shift + rng.normal(0, run_sd, (SIMS, n, k)) + rng.normal(0, judge_sd, (SIMS, n, k))
        return np.clip(np.rint(x), 0, 10).mean(axis=2)

    return score(eff) - score(0.0)


# --- pass/fail (tests pass, defect found) -----------------------------------------------
# per-case baseline pass probability p_i ~ Beta(a, b); B adds a logit shift chosen so the
# MEAN pass rate rises by `gain`. U-shaped Beta(0.5, 0.5): tasks are mostly always/never
# solved (typical of SWE tasks). Uniform Beta(1, 1): more cases are coin flips.
def logit(p):
    return np.log(p / (1 - p))


def shift_for(gain: float, a: float, b: float) -> float:
    p = np.clip(rng.beta(a, b, 200_000), 1e-4, 1 - 1e-4)
    base = p.mean()
    lo, hi = -5.0, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        m = (1 / (1 + np.exp(-(logit(p) + mid)))).mean()
        lo, hi = (mid, hi) if m - base < gain else (lo, mid)
    return (lo + hi) / 2


def pass_diffs(n: int, k: int, shift: float, a: float, b: float):
    p = np.clip(rng.beta(a, b, size=(SIMS, n, 1)), 1e-4, 1 - 1e-4)
    pb = 1 / (1 + np.exp(-(logit(p) + shift)))
    ya = (rng.random((SIMS, n, k)) < p).mean(axis=2)
    yb = (rng.random((SIMS, n, k)) < pb).mean(axis=2)
    return yb - ya


def table(title, rows):
    print(f"\n{title}")
    for r in rows:
        print("  " + "  ".join(f"{c:>10}" for c in r))


def decide_signflip(d: np.ndarray, margin: float = 0.0, perms: int = 2000) -> np.ndarray:
    """Paired sign-flip randomisation test of mean(d - margin) > 0, one-sided 5 %.
    Exact-size under exchangeability of A and B within a case (what randomised order and
    blinding give us), whatever the shape of the distribution."""
    sims, n = d.shape
    x = d - margin
    signs = rng.choice([-1.0, 1.0], size=(perms, n))
    out = np.empty(sims, dtype=bool)
    for s in range(sims):
        obs = x[s].mean()
        null = (signs * x[s]).mean(axis=1)
        out[s] = (1 + (null >= obs - 1e-12).sum()) / (perms + 1) <= 0.05
    return out


if __name__ == "__main__":
    import sys

    if "--signflip" in sys.argv:
        rows = [["metric", "n", "k", "boot power", "flip power", "boot alpha", "flip alpha"]]
        for n, k in ((10, 1), (10, 3), (20, 1), (20, 3), (30, 3)):
            d1, d0 = rubric_diffs(n, k, 1.0), rubric_diffs(n, k, 0.0)
            rows.append(["rubric d=1", n, k, f"{decide(d1, 0).mean():.2f}",
                         f"{decide_signflip(d1).mean():.2f}", f"{decide(d0, 0).mean():.3f}",
                         f"{decide_signflip(d0).mean():.3f}"])
        s10 = shift_for(0.10, 0.5, 0.5)
        for n, k in ((20, 3), (40, 3), (60, 3)):
            d1, d0 = pass_diffs(n, k, s10, 0.5, 0.5), pass_diffs(n, k, 0.0, 0.5, 0.5)
            rows.append(["pass +10pp", n, k, f"{decide(d1, 0).mean():.2f}",
                         f"{decide_signflip(d1).mean():.2f}", f"{decide(d0, 0).mean():.3f}",
                         f"{decide_signflip(d0).mean():.3f}"])
        table("BOOTSTRAP vs SIGN-FLIP decision (one-sided 5 %)", rows)
        sys.exit()

    ns = [10, 20, 30, 40, 60]

    rows = [["n cases", "k", "power d=1", "power d=.5", "alpha d=0", "sd(d_i)"]]
    for k in (1, 3):
        for n in ns:
            p1 = decide(rubric_diffs(n, k, 1.0), 0.0).mean()
            p5 = decide(rubric_diffs(n, k, 0.5), 0.0).mean()
            d0 = rubric_diffs(n, k, 0.0)
            a0 = decide(d0, 0.0).mean()
            rows.append([n, k, f"{p1:.2f}", f"{p5:.2f}", f"{a0:.3f}", f"{d0.std(axis=1).mean():.2f}"])
    table("RUBRIC superiority (one-sided 5 %), run sd .8, judge sd .7, effect het .5", rows)

    rows = [["n cases", "k", "NI power", "NI alpha"]]
    for k in (1, 3):
        for n in (10, 20, 30, 40):
            # same agent, optimisation on: true effect 0 -> want 'not worse than -0.5'
            pw = decide(rubric_diffs(n, k, 0.0, het_sd=0.3), -0.5).mean()
            al = decide(rubric_diffs(n, k, -0.5, het_sd=0.3), -0.5).mean()
            rows.append([n, k, f"{pw:.2f}", f"{al:.3f}"])
    table("RUBRIC non-inferiority, margin -0.5 (M3-24 TokenLean A/B)", rows)

    for a, b, label in ((0.5, 0.5, "U-shaped Beta(.5,.5)"), (1.0, 1.0, "uniform Beta(1,1)")):
        s10 = shift_for(0.10, a, b)
        s20 = shift_for(0.20, a, b)
        rows = [["n cases", "k", "power +10pp", "power +20pp", "alpha 0"]]
        for k in (1, 3, 5):
            for n in (20, 40, 60, 100, 150):
                p10 = decide(pass_diffs(n, k, s10, a, b), 0.0).mean()
                p20 = decide(pass_diffs(n, k, s20, a, b), 0.0).mean()
                a0 = decide(pass_diffs(n, k, 0.0, a, b), 0.0).mean()
                rows.append([n, k, f"{p10:.2f}", f"{p20:.2f}", f"{a0:.3f}"])
        table(f"PASS RATE superiority, baseline mean .5, {label}", rows)

    # absolute-gate precision (M1-64): half-width of the Wilson 95 % interval at p = .7 / .5
    rows = [["n", "p=.5 +/-", "p=.7 +/-"]]
    for n in (10, 20, 40, 60, 100):
        hw = []
        for p in (0.5, 0.7):
            z = 1.96
            den = 1 + z * z / n
            hw.append(z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den)
        rows.append([n, f"{hw[0]:.2f}", f"{hw[1]:.2f}"])
    table("ABSOLUTE GATE: Wilson 95 % half-width (M1-64 recall / pass-rate targets)", rows)
