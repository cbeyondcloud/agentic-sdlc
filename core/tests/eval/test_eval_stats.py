"""The eval harness's statistics (M1-63, spike S10): stdlib only, checked against scipy.

`stats_vectors.json` holds inputs and scipy 1.18.1's answers, generated in the Linux
container by `gen_vectors.py` (scipy is never installed on developer laptops)."""

import json
import random
from pathlib import Path

import pytest

from agentic.eval import stats
from agentic.eval.stats import Unmeasurable

VECTORS = json.loads((Path(__file__).parent / "stats_vectors.json").read_text(encoding="utf-8"))
TOLERANCE = 1e-12


def test_empty_comparisons_are_unmeasurable_not_ties():
    for call in (
        lambda: stats.signflip_test([]),
        lambda: stats.paired_bootstrap([]),
        lambda: stats.wilson(0, 0),
        lambda: stats.cluster_bootstrap_ratio([]),
    ):
        with pytest.raises(Unmeasurable):
            call()


def test_signflip_detects_a_clear_effect_and_not_noise():
    rng = random.Random(3)
    effect = [1.0 + rng.gauss(0, 0.5) for _ in range(20)]
    noise = [rng.gauss(0, 1.0) for _ in range(20)]
    assert stats.signflip_test(effect).p_one_sided < 0.01
    assert stats.signflip_test(noise).p_one_sided > 0.05


def test_signflip_margin_turns_superiority_into_non_inferiority():
    diffs = [0.1, -0.2, 0.0, 0.2, -0.1, 0.05, -0.05, 0.1, -0.1, 0.0] * 3
    assert stats.signflip_test(diffs).p_one_sided > 0.05  # not better
    assert stats.signflip_test(diffs, margin=-0.5).p_one_sided < 0.01  # not worse than -0.5


def test_random_procedures_are_reproducible_from_their_seed():
    diffs = [0.5, 1.0, -0.5, 2.0, 0.0, 1.5] * 5
    assert stats.signflip_test(diffs, seed=7) == stats.signflip_test(diffs, seed=7)
    assert stats.paired_bootstrap(diffs, seed=7) == stats.paired_bootstrap(diffs, seed=7)
    assert stats.paired_bootstrap(diffs, seed=7) != stats.paired_bootstrap(diffs, seed=8)


def test_bootstrap_interval_brackets_the_mean():
    diffs = [1.0, 2.0, 0.0, 1.5, 0.5, 1.0, 2.5, -0.5]
    result = stats.paired_bootstrap(diffs)
    assert result.ci_low < result.mean < result.ci_high
    assert result.as_dict()["n"] == len(diffs)


def test_per_case_means_skip_unmeasured_runs_and_name_missing_cases():
    a = {"c1": [6.0, 8.0, None], "c2": [None, None], "c3": [5.0]}
    b = {"c1": [9.0, 9.0, 9.0], "c2": [7.0], "c4": [1.0]}
    assert stats.per_case(a) == {"c1": 7.0, "c3": 5.0}
    diffs, unscored = stats.paired_diffs(a, b)
    assert diffs == [2.0]  # only c1 is scored on both sides
    assert unscored == ["c2", "c3", "c4"]


def test_cluster_bootstrap_ratio_pools_by_case():
    pairs = [(2, 2), (1, 3), (0, 2), (3, 3)]  # (caught, total) per case
    result = stats.cluster_bootstrap_ratio(pairs)
    assert result.mean == pytest.approx(6 / 10)
    assert result.ci_low <= result.mean <= result.ci_high


def test_matches_scipy_vectors():
    checked = 0
    for vector in VECTORS["sign_test"]:
        assert stats.sign_test(vector["diffs"]).p_two_sided == pytest.approx(
            vector["p"], abs=TOLERANCE
        )
        checked += 1
    for vector in VECTORS["wilcoxon"]:
        result = stats.wilcoxon_signed_rank(vector["diffs"])
        assert result.method == vector["method"]
        assert result.p_two_sided == pytest.approx(vector["p"], abs=TOLERANCE)
        checked += 1
    for vector in VECTORS["wilson"]:
        low, high = stats.wilson(vector["k"], vector["n"])
        assert (low, high) == pytest.approx((vector["low"], vector["high"]), abs=TOLERANCE)
        checked += 1
    for vector in VECTORS["signflip_exact"]:
        result = stats.signflip_test(vector["diffs"], margin=vector["margin"])
        assert result.exact
        assert result.p_one_sided == pytest.approx(vector["p"], abs=TOLERANCE)
        checked += 1
    for vector in VECTORS["bh"]:
        adjusted = stats.bh_adjusted(vector["p"])
        assert adjusted == pytest.approx(vector["adjusted"], abs=TOLERANCE)
        checked += 1
    for vector in VECTORS["pearson"]:
        assert stats.pearson(vector["x"], vector["y"]) == pytest.approx(vector["r"], abs=TOLERANCE)
        checked += 1
    for vector in VECTORS["kappa"]:
        value = stats.weighted_kappa(vector["a"], vector["b"], categories=vector["categories"])
        assert value == pytest.approx(vector["kappa"], abs=TOLERANCE)
        checked += 1
    assert checked >= 300


def test_benjamini_hochberg_rejects_at_q():
    pvalues = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    # BH at q=0.05 rejects the two smallest only (classic textbook example)
    assert stats.benjamini_hochberg(pvalues, q=0.05) == [True, True] + [False] * 8
    assert stats.benjamini_hochberg([], q=0.1) == []


def test_weighted_kappa_perfect_and_chance():
    scores = [0, 1, 2, 3, 4, 5]
    assert stats.weighted_kappa(scores, scores, categories=range(11)) == pytest.approx(1.0)
    with pytest.raises(Unmeasurable):
        stats.weighted_kappa([], [], categories=range(3))
    with pytest.raises(ValueError):
        stats.weighted_kappa([1, 2], [1], categories=range(3))


def test_pearson_is_none_without_variance():
    assert stats.pearson([1, 1, 1], [1, 2, 3]) is None
    assert stats.pearson([1, 2], [2, 4]) is None  # too few points to mean anything
