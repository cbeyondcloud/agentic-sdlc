"""The release SBOM, third-party notices and constraints (M3-21): generated from uv.lock,
with each dependency's licence from the data scripts/licence_check.py already reads."""

import json
import sys
from pathlib import Path

import pytest
from release_support import REPO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_release import sbom  # noqa: E402

EPOCH = 1_790_000_000  # 2026-09-21T14:13:20Z
SHIPPED = {
    "agentic-cli",
    "agentic-core",
    "sdlc-commons",
    "agentic-control-plane",
    "pydantic",
    "fastapi",
    "sdlc-harness",
    "code-review-graph",  # an optional extra the org may install
}
DEV_ONLY = {"pytest", "ruff", "pip-audit", "import-linter", "pre-commit", "trustme"}


@pytest.fixture(scope="module")
def bom() -> dict:
    return sbom.export_sbom(REPO, "0.1.0", EPOCH)


def components(bom: dict) -> dict[str, dict]:
    return {c["name"]: c for c in bom["components"]}


def test_sbom_validates_against_cyclonedx_1_5(bom: dict):
    from cyclonedx.schema import SchemaVersion
    from cyclonedx.validation.json import JsonStrictValidator

    errors = JsonStrictValidator(SchemaVersion.V1_5).validate_str(sbom.dumps(bom))
    assert errors is None, errors


def test_sbom_names_every_shipped_package_and_no_dev_tool(bom: dict):
    names = set(components(bom))
    assert SHIPPED <= names
    assert not DEV_ONLY & names
    assert "agentic-sdlc-workspace" not in names  # the unpublished root project
    refs = {c["bom-ref"] for c in bom["components"]} | {bom["metadata"]["component"]["bom-ref"]}
    for entry in bom["dependencies"]:
        assert entry["ref"] in refs and set(entry.get("dependsOn", [])) <= refs
    assert bom["metadata"]["component"]["name"] == "agentic-sdlc"
    assert bom["metadata"]["component"]["version"] == "0.1.0"


def test_sbom_has_licences(bom: dict):
    found = components(bom)
    assert found["agentic-cli"]["licenses"] == [{"expression": "MIT"}]
    assert found["agentic-cli"]["purl"] == "pkg:pypi/agentic-cli@0.1.0"
    assert found["pydantic"]["licenses"] == [{"expression": "MIT"}]
    # only a "MIT License" classifier: the SPDX id; free text defers to the classifiers
    assert found["mdurl"]["licenses"] == [{"expression": "MIT"}]
    assert "Apache" in found["python-dateutil"]["licenses"][0]["license"]["name"]
    # no licence metadata in the wheel: the reason recorded in licence-exceptions.toml
    assert found["cel-python"]["licenses"] == [{"expression": "Apache-2.0"}]
    harness = found["sdlc-harness"]["licenses"][0]["license"]["name"]
    assert "Zyroflo" in harness
    unchecked = [
        c["name"]
        for c in bom["components"]
        if "licenses" not in c
        and not any(p["name"] == "agentic:licence" for p in c.get("properties", []))
    ]
    assert unchecked == []


def test_sbom_is_deterministic(bom: dict):
    again = sbom.export_sbom(REPO, "0.1.0", EPOCH)
    assert sbom.dumps(again) == sbom.dumps(bom)
    assert bom["metadata"]["timestamp"] == "2026-09-21T14:13:20Z"
    assert bom["serialNumber"].startswith("urn:uuid:")
    other = sbom.export_sbom(REPO, "0.2.0", EPOCH)
    assert other["serialNumber"] != bom["serialNumber"]


def test_notices_cover_every_component_and_the_adapted_code(bom: dict):
    text = sbom.notices(bom, REPO)
    assert text.startswith("# Third-party notices for agentic-sdlc 0.1.0\n")
    for name in components(bom):
        assert f"| {name} |" in text, name
    assert "OpenHands" in text and "spec-kit" in text  # THIRD_PARTY_NOTICES.md
    assert "### pydantic " in text and "Permission is hereby granted" in text
    assert b"\r\n" not in text.encode()


def test_constraints_pin_third_party_packages_only():
    text = sbom.constraints(REPO)
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert any(ln.startswith("pydantic==") for ln in lines)
    assert "sdlc-harness==0.1.0" in lines
    assert not [ln for ln in lines if "git+" in ln or ln.startswith(("-e", "./", "agentic-"))]
    assert not [ln for ln in lines if ln.startswith(("pytest==", "ruff=="))]


def test_dumps_is_stable_json(bom: dict):
    text = sbom.dumps(bom)
    assert text.endswith("}\n") and json.loads(text) == bom
