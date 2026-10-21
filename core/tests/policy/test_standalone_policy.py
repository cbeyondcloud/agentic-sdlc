"""Tests for the standalone policy template, `templates/standalone/` (M1-62).

A person without an org copies it to `~/.agentic/` (`agentic init --standalone`, M1-53),
edits the placeholder models and runs the library agents and pipelines with it.
"""

import re
from pathlib import Path

import pytest

from agentic.policy import load_policy
from agentic.skills import PreloadRule, load_catalog
from agentic.yamlio import load_yaml, read_text

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "templates" / "standalone"
#: Vendor, model-family and hosting names that must never be a default (provider-agnostic).
VENDORS = re.compile(
    r"anthropic|claude|openai|\bgpt|codex|gemini|google|vertex|bedrock|azure|"
    r"ollama|llama|mistral|qwen|deepseek|cohere|grok",
    re.IGNORECASE,
)
BUILTIN_TYPES = {
    "business-analyst": "requirements",
    "design": "design",
    "developer": "dev-report",
    "code-review": "review",
    "qa": "test-report",
}


@pytest.fixture(scope="module")
def policy():
    return load_policy(TEMPLATE / "policy")


def test_template_loads(policy):
    assert policy.default_classification in policy.classifications
    assert set(BUILTIN_TYPES) | {"general"} <= set(policy.agent_types)
    assert policy.version.startswith(("git:", "sha256:"))


def test_types_fix_the_builtin_kinds(policy):
    assert {name: policy.agent_types[name].produces for name in BUILTIN_TYPES} == BUILTIN_TYPES


def test_tiers_are_capability_names_with_placeholder_models(policy):
    assert set(policy.tiers) == {"deep-reasoning", "balanced", "fast", "local"}
    for name, tier in policy.tiers.items():
        assert "change-me" in tier.model, name  # the user edits every one


def test_no_vendor_names():
    found = {
        path.relative_to(TEMPLATE).as_posix(): VENDORS.findall(read_text(path))
        for path in TEMPLATE.rglob("*")
        if path.is_file()
    }
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_preload_rule_names_the_local_tier(policy):
    """Decision 34: the standalone template names its local models, so weak or local
    models get their skills pre-loaded instead of discovered."""
    data, _ = load_yaml(read_text(TEMPLATE / "skills" / "preload.yaml"))
    rule = PreloadRule.model_validate(data["preload_for"])
    assert rule.matches("local", policy.tiers["local"].model)
    for name in ("deep-reasoning", "balanced", "fast"):
        assert not rule.matches(name, policy.tiers[name].model), name


def test_agent_type_skills_are_in_the_default_catalog(policy):
    catalog = load_catalog(REPO / "library" / "skills")
    for name, agent_type in policy.agent_types.items():
        missing = [skill for skill in agent_type.skills if skill not in catalog.skills]
        assert missing == [], name
        pre = [s for s in agent_type.skills if catalog.skills[s].loading == "pre"]
        assert len(pre) <= 3, name  # spec §11.3: few pre-loaded skills per role


def test_safe_defaults(policy):
    assert {"team", "regulated"} <= set(policy.sandbox.required_for)  # DF-72
    assert policy.secret_scan.action == "block"
    assert policy.allow_repo_pipelines  # standalone mode keeps local pipelines (D33)
    assert policy.allowed_repo_patterns == []  # any repo: there is no org to name them
    assert "git push" in policy.deny.shell  # publishing is the pipeline's action, never an agent's
    assert policy.classifications["restricted"].allowed_tiers == ["local"]
