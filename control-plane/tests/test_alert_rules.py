"""The Prometheus alert rules (M2-12): every metric a rule names is one the control plane
exports, every rule says how bad it is and what happened, and `promtool` accepts the file
and its unit tests. promtool is a native binary: it runs only in the Linux check image
(`scripts/check.py --linux`), never on a developer laptop, so elsewhere that test skips."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from prometheus_client.metrics_core import Metric

from agentic.yamlio import load_yaml
from agentic_cp.metrics import Metrics
from agentic_cp.settings import Settings

RULES_DIR = Path(__file__).resolve().parents[1] / "deploy" / "prometheus"
RULES = RULES_DIR / "alerts.yml"
RULE_TESTS = RULES_DIR / "alerts.test.yml"
SEVERITIES = {"critical", "warning", "info"}
NO_PROMTOOL = "promtool runs only in the Linux check image: scripts/check.py --linux"


def _rules() -> list[dict]:
    data, _ = load_yaml(RULES.read_text(encoding="utf-8"))
    return [rule for group in data["groups"] for rule in group["rules"]]


def _exported_names() -> set[str]:
    metrics = Metrics(Settings.from_env({"AGENTIC_CP_DATABASE_URL": "postgresql://x@h/db"}))
    names: set[str] = set()
    for family in metrics.registry.collect():
        family: Metric
        names.add(family.name)
        if family.type == "counter":
            names.add(f"{family.name}_total")
        if family.type == "histogram":
            names.update(f"{family.name}_{suffix}" for suffix in ("bucket", "count", "sum"))
    return names


def test_every_rule_names_exported_metrics():
    exported = _exported_names()
    for rule in _rules():
        for name in re.findall(r"\bagentic_cp_[a-z0-9_]+", rule["expr"]):
            assert name in exported, (rule["alert"], name)


def test_every_rule_has_severity_and_summary():
    names = []
    for rule in _rules():
        names.append(rule["alert"])
        assert rule["alert"].startswith("Agentic"), rule["alert"]
        assert rule["labels"]["severity"] in SEVERITIES, rule["alert"]
        assert rule["annotations"]["summary"].strip(), rule["alert"]
        assert rule["annotations"]["description"].strip(), rule["alert"]
    assert len(names) == len(set(names))
    for wanted in (
        "AgenticControlPlaneDown",
        "AgenticControlPlaneHighErrorRate",
        "AgenticIngestLagHigh",
        "AgenticIngestChainRejections",
        "AgenticOutboxBacklog",
        "AgenticOutboxDeliveryStuck",
        "AgenticOutboxFailures",
        "AgenticAutoTripActive",
    ):
        assert wanted in names


def test_rules_carry_no_unbounded_labels():
    """Rules aggregate by tenant at most (spec §14.3): never by run, user or token."""
    for rule in _rules():
        for grouping in re.findall(r"by \(([^)]*)\)", rule["expr"]):
            labels = {label.strip() for label in grouping.split(",")}
            assert labels <= {"tenant", "le", "mode", "source", "job", "instance"}, rule["alert"]


#: Gauges read from the database: every replica exports the same value.
DB_GAUGES = (
    "agentic_cp_outbox_pending",
    "agentic_cp_outbox_failed",
    "agentic_cp_outbox_oldest_pending_seconds",
    "agentic_cp_killswitches_active",
)


def test_database_gauges_are_never_summed_across_replicas():
    """Review F3: each replica reads the same database, so `sum by (tenant)` multiplies a
    tenant's backlog (or its auto-tripped teams) by the replica count. The aggregation
    nearest each database gauge must be `max` (alerts.test.yml runs two replicas)."""
    aggregation = re.compile(r"\b(sum|max|min|avg|count)\s+(?:by|without)\s*\([^)]*\)\s*\(")
    checked = 0
    for rule in _rules():
        expr = rule["expr"]
        for name in DB_GAUGES:
            for found in re.finditer(rf"\b{name}\b", expr):
                nearest = list(aggregation.finditer(expr, 0, found.start()))
                assert nearest, (rule["alert"], name)
                assert nearest[-1].group(1) == "max", (rule["alert"], name)
                checked += 1
    assert checked >= 4


def test_promtool_accepts_the_rules():
    promtool = shutil.which("promtool")
    if promtool is None:
        if os.environ.get("AGENTIC_CHECK_IMAGE") == "1":
            pytest.fail("the Linux check image must have promtool (scripts/linux-check.Dockerfile)")
        pytest.skip(NO_PROMTOOL)
    checked = subprocess.run(
        [promtool, "check", "rules", str(RULES)], capture_output=True, text=True, timeout=60
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    tested = subprocess.run(
        [promtool, "test", "rules", RULE_TESTS.name],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=RULES_DIR,
    )
    assert tested.returncode == 0, tested.stdout + tested.stderr
