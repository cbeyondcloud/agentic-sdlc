"""Shared paths and fixtures for the library tests (a unique module name, so they never
collide with another folder's conftest)."""

from pathlib import Path

import pytest

from agentic.definitions import DefinitionSet
from agentic.policy import load_policy

REPO = Path(__file__).resolve().parents[3]
LIBRARY = REPO / "library"
STANDALONE_POLICY = REPO / "templates" / "standalone" / "policy"
#: Vendor, model-family and hosting names that never appear in shipped defaults.
VENDOR_NAMES = (
    r"anthropic|claude|openai|\bgpt|codex|gemini|google|vertex|bedrock|"
    r"ollama|llama|mistral|qwen|deepseek|cohere|grok"
)


@pytest.fixture(scope="module")
def library() -> DefinitionSet:
    """The library's agents and pipelines, read as the org layer."""
    return DefinitionSet.discover(org=LIBRARY, team=None, repo=None)


@pytest.fixture(scope="module")
def standalone_policy():
    return load_policy(STANDALONE_POLICY)
