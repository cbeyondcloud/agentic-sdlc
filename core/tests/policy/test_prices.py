"""Tests for model prices in org policy, used to estimate costs TokenLean does not report."""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from agentic.policy import ModelPrice, PolicyError, load_policy


@pytest.fixture
def policy_dir(fixtures: Path, tmp_path: Path) -> Path:
    target = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", target)
    return target


def test_prices_load_as_exact_decimals(policy_dir: Path):
    price = load_policy(policy_dir).prices["claude-sonnet-5-5"]

    assert price == ModelPrice(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15"),
                               cache_read_per_mtok=Decimal("0.3"),
                               cache_write_per_mtok=Decimal("3.75"))  # fmt: skip
    assert isinstance(price.cache_read_per_mtok, Decimal)


def test_an_exact_name_wins(policy_dir: Path):
    policy = load_policy(policy_dir)

    assert policy.price_for("gpt-5") == policy.prices["gpt-5"]


def test_otherwise_the_longest_contained_name_wins(policy_dir: Path):
    policy = load_policy(policy_dir)

    assert policy.price_for("openai/gpt-5-mini-2026-08") == policy.prices["gpt-5-mini"]
    assert policy.price_for("Anthropic/Claude-Sonnet-5-5") == policy.prices["claude-sonnet-5-5"]


def test_an_unknown_model_has_no_price_never_a_default(policy_dir: Path):
    assert load_policy(policy_dir).price_for("mystery-model-1") is None


def test_prices_are_optional(policy_dir: Path):
    (policy_dir / "prices.yaml").unlink()

    assert load_policy(policy_dir).prices == {}


@pytest.mark.parametrize("entry", [
    "{ input_per_mtok: -1, output_per_mtok: 2 }",
    "{ input_per_mtok: 1 }",
    "{ input_per_mtok: 1, output_per_mtok: 2, surprise: 3 }",
    "{ input_per_mtok: .nan, output_per_mtok: 2 }",
])  # fmt: skip
def test_a_bad_price_is_refused(policy_dir: Path, entry: str):
    (policy_dir / "prices.yaml").write_text(f"prices:\n  bad-model: {entry}\n", encoding="utf-8")

    with pytest.raises(PolicyError, match="bad-model"):
        load_policy(policy_dir)
