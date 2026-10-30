"""The chart's environments (M3-04): one values file each for dev, staging and prod under
`deploy/helm/agentic-platform/environments/`, the optional External Secrets and cert-manager
objects, the scheduled logical backup, and the guards that keep prod from installing without
TLS, managed secrets or a pinned image. Rendered with the pinned helm and validated with
kubeconform as in test_helm_chart.py (scripts/tests/helm_support.py says where they run).

Review Focus: each environment tracks its own policy branch (dev, staging, main); prod
refuses plaintext TLS, an unmanaged Secret and a tag-only image; the ExternalSecret names
remote keys only and exists before the migration hook needs its Secret."""

from __future__ import annotations

from typing import Any

import pytest
from helm_support import CHART, KUBERNETES_VERSIONS, ROOT, manifests, tools
from ruamel.yaml import YAML

RELEASE = "agentic"
NAMESPACE = "agentic"
SECRET = "agentic-platform"
ENVIRONMENTS = ("dev", "staging", "prod")
#: What an operator adds at install time: prod refuses an image without a digest.
DIGEST = "sha256:" + "1" * 64
#: What an operator adds at install time: prod refuses an image without a digest, and a
#: NetworkPolicy the platform team has not confirmed its CNI enforces.
INSTALL = {
    "dev": [],
    "staging": [],
    "prod": ["--set", f"image.digest={DIGEST}", "--set", "networkPolicy.cniEnforces=true"],
}
BRANCHES = {"dev": "dev", "staging": "staging", "prod": "main"}
RUNBOOK = ROOT / "docs" / "runbooks" / "environments-and-promotion.md"


@pytest.fixture(scope="module")
def chart_tools():
    return tools()


def _values(env: str) -> str:
    return f"agentic-platform/environments/{env}.yaml"


def _template(chart_tools, *args: str):
    return chart_tools.helm(
        ["template", RELEASE, "agentic-platform", "--namespace", NAMESPACE, *args]
    )


@pytest.fixture(scope="module")
def renders(chart_tools) -> dict[str, str]:
    out = {}
    for env in ENVIRONMENTS:
        done = _template(chart_tools, "-f", _values(env), *INSTALL[env])
        assert done.returncode == 0, f"{env}: {done.stderr}"
        out[env] = done.stdout
    return out


def _one(objects, kind: str, component: str | None = None) -> dict[str, Any]:
    found = [
        o
        for o in objects
        if o["kind"] == kind
        and (
            component is None
            or o["metadata"]["labels"].get("app.kubernetes.io/component") == component
        )
    ]
    assert len(found) == 1, f"{kind}/{component}: {len(found)} objects"
    return found[0]


def _env(obj: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if obj["kind"] == "CronJob":
        spec = obj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    else:
        spec = obj["spec"]["template"]["spec"]
    return {item["name"]: item for item in spec["containers"][0].get("env", [])}


def _prod_without(chart_tools, *overrides: str):
    return _template(chart_tools, "-f", _values("prod"), *INSTALL["prod"], *overrides)


# ── every environment ─────────────────────────────────────────────────────────


def test_there_is_one_values_file_per_environment():
    found = sorted(p.stem for p in (CHART / "environments").glob("*.yaml"))
    assert found == sorted(ENVIRONMENTS)
    for env in ENVIRONMENTS:
        values = YAML(typ="safe", pure=True).load(
            (CHART / "environments" / f"{env}.yaml").read_text("utf-8")
        )
        assert values["environment"]["name"] == env


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_each_environment_renders_without_a_secret_value(renders, env):
    objects = manifests(renders[env])
    assert not [o for o in objects if o["kind"] == "Secret"]
    assert "postgresql://" not in renders[env]
    _one(objects, "Deployment", "api")
    _one(objects, "Deployment", "worker")
    _one(objects, "Job", "migrate")


def test_the_policy_branch_differs_per_environment(renders):
    seen = {}
    for env in ENVIRONMENTS:
        objects = manifests(renders[env])
        for component in ("api", "worker"):
            env_vars = _env(_one(objects, "Deployment", component))
            seen.setdefault(env, set()).add(env_vars["AGENTIC_CP_POLICY_BRANCH"]["value"])
    assert seen == {env: {branch} for env, branch in BRANCHES.items()}


def test_without_an_environment_no_branch_is_pinned(chart_tools):
    done = _template(chart_tools)
    assert done.returncode == 0, done.stderr
    api = _one(manifests(done.stdout), "Deployment", "api")
    assert "AGENTIC_CP_POLICY_BRANCH" not in _env(api)


def test_a_policy_branch_can_be_named_explicitly(chart_tools):
    done = _template(
        chart_tools, "-f", _values("staging"), "--set", "environment.policyBranch=release/next"
    )
    assert done.returncode == 0, done.stderr
    api = _one(manifests(done.stdout), "Deployment", "api")
    assert _env(api)["AGENTIC_CP_POLICY_BRANCH"]["value"] == "release/next"


def test_an_unknown_environment_is_refused(chart_tools):
    done = _template(chart_tools, "--set", "environment.name=production")
    assert done.returncode != 0
    assert "environment.name" in done.stderr and "prod" in done.stderr


def test_the_policy_branch_cannot_be_set_through_config(chart_tools):
    done = _template(chart_tools, "--set", "config.AGENTIC_CP_POLICY_BRANCH=main")
    assert done.returncode != 0
    assert "environment.policyBranch" in done.stderr


def test_staging_rehearses_prod_at_small_scale_on_kind(renders):
    # decision 114: staging is a local kind cluster; it still runs two API replicas and a
    # standby worker, so upgrades and the restore drill are rehearsed on the prod shape
    objects = manifests(renders["staging"])
    assert _one(objects, "HorizontalPodAutoscaler", "api")["spec"]["minReplicas"] >= 2
    assert _one(objects, "Deployment", "worker")["spec"]["replicas"] == 2
    kinds = {o["kind"] for o in objects}
    assert not kinds & {"ExternalSecret", "Certificate", "Ingress"}, "kind has no such controllers"
    _one(objects, "CronJob", "backup")  # the daily backup the restore drill reads


def test_dev_is_small(renders):
    objects = manifests(renders["dev"])
    assert _one(objects, "Deployment", "api")["spec"]["replicas"] == 1
    assert not [
        o for o in objects if o["kind"] in ("HorizontalPodAutoscaler", "PodDisruptionBudget")
    ]


# ── prod ──────────────────────────────────────────────────────────────────────


def test_prod_serves_tls_from_cert_manager(renders):
    objects = manifests(renders["prod"])
    ingress = _one(objects, "Ingress", "api")
    (tls,) = ingress["spec"]["tls"]
    certificate = _one(objects, "Certificate")
    assert certificate["apiVersion"] == "cert-manager.io/v1"
    assert certificate["spec"]["secretName"] == tls["secretName"]
    assert certificate["spec"]["dnsNames"] == tls["hosts"] == [ingress["spec"]["rules"][0]["host"]]
    assert certificate["spec"]["issuerRef"]["kind"] in ("ClusterIssuer", "Issuer")
    assert certificate["spec"]["issuerRef"]["name"]


def test_prod_reads_its_secret_from_external_secrets(renders):
    objects = manifests(renders["prod"])
    external = _one(objects, "ExternalSecret")
    assert external["apiVersion"] == "external-secrets.io/v1"
    spec = external["spec"]
    assert spec["target"]["name"] == SECRET
    # re-creating the hook must never delete the Secret the running pods read
    assert spec["target"]["creationPolicy"] == "Orphan"
    assert spec["secretStoreRef"]["name"]
    keys = {item["secretKey"]: item["remoteRef"] for item in spec["data"]}
    assert {"database-url", "owner-database-url", "backup-database-url"} <= keys.keys()
    assert all(ref["key"] and "://" not in ref["key"] for ref in keys.values())
    # the migration hook (weight -5) needs the Secret: the ExternalSecret comes first
    annotations = external["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert int(annotations["helm.sh/hook-weight"]) < -5


def test_prod_limits_egress_with_a_network_policy(renders):
    # DF-805: the network-level guard for tenant-set git URLs is on in prod
    policy = _one(manifests(renders["prod"]), "NetworkPolicy", "api")
    assert "Egress" in policy["spec"]["policyTypes"]
    blocked = [
        rule["to"][0]["ipBlock"].get("except", [])
        for rule in policy["spec"]["egress"]
        if rule.get("to") and "ipBlock" in rule["to"][0]
    ]
    assert any("169.254.0.0/16" in excepted and "10.0.0.0/8" in excepted for excepted in blocked)


def test_prod_runs_the_sized_api(renders):
    # master plan sizing: 3 replicas x 1 vCPU / 1 GB, scaled out at about 60 % CPU
    objects = manifests(renders["prod"])
    hpa = _one(objects, "HorizontalPodAutoscaler", "api")
    assert hpa["spec"]["minReplicas"] >= 3
    assert hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 60
    api = _one(objects, "Deployment", "api")
    assert api["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] == {
        "cpu": "1",
        "memory": "1Gi",
    }
    assert api["spec"]["template"]["spec"]["containers"][0]["image"].endswith("@" + DIGEST)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (["--set", "ingress.tlsSecretName="], "TLS"),
        (["--set", "ingress.enabled=false"], "TLS"),
        (["--set", "publicUrl=http://agentic.example.org"], "https://"),
        (["--set", "externalSecrets.enabled=false"], "externalSecrets.enabled"),
        (["--set", "image.digest="], "image.digest"),
    ],
    ids=["no-tls-secret", "no-ingress", "plain-http", "plaintext-secret", "tag-only"],
)
def test_prod_refuses_an_unsafe_install(chart_tools, overrides, message):
    done = _prod_without(chart_tools, *overrides)
    assert done.returncode != 0
    assert message in done.stderr


def test_prod_accepts_a_secret_managed_by_another_tool(chart_tools):
    # Sealed Secrets or a CSI driver: the operator says so explicitly
    done = _prod_without(
        chart_tools,
        "--set",
        "externalSecrets.enabled=false",
        "--set",
        "secrets.externallyManaged=true",
    )
    assert done.returncode == 0, done.stderr
    assert not [o for o in manifests(done.stdout) if o["kind"] == "ExternalSecret"]


# ── the optional objects on their own ─────────────────────────────────────────


def test_external_secrets_need_a_store_and_both_dsns(chart_tools):
    done = _template(chart_tools, "--set", "externalSecrets.enabled=true")
    assert done.returncode != 0
    assert "externalSecrets.secretStoreRef.name" in done.stderr
    done = _template(
        chart_tools,
        "--set",
        "externalSecrets.enabled=true",
        "--set",
        "externalSecrets.secretStoreRef.name=vault",
        "--set",
        "externalSecrets.remoteRefs.databaseUrl.key=agentic/dsn",
    )
    assert done.returncode != 0
    assert "externalSecrets.remoteRefs.ownerDatabaseUrl" in done.stderr


def test_cert_manager_needs_the_ingress_and_an_issuer(chart_tools):
    done = _template(chart_tools, "--set", "ingress.certManager.enabled=true")
    assert done.returncode != 0
    assert "ingress.enabled" in done.stderr
    done = _template(
        chart_tools,
        "--set",
        "ingress.enabled=true",
        "--set",
        "ingress.host=agentic.example.org",
        "--set",
        "ingress.tlsSecretName=agentic-tls",
        "--set",
        "ingress.certManager.enabled=true",
    )
    assert done.returncode != 0
    assert "ingress.certManager.issuerRef.name" in done.stderr


def test_the_backup_cronjob_dumps_the_schema_as_the_backup_role(renders):
    # found on kind: the owner role cannot dump, every tenant table forces row-level security
    # DF-799: the daily logical backup, the file the restore drill restores
    cron = _one(manifests(renders["staging"]), "CronJob", "backup")
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    script = " ".join(container["args"])
    assert 'pg_dump -Fc --schema="agentic"' in script
    env = _env(cron)
    assert env["AGENTIC_BACKUP_DSN"]["valueFrom"]["secretKeyRef"] == {
        "name": SECRET,
        "key": "backup-database-url",
    }
    assert "@sha256:" in container["image"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["securityContext"]["runAsNonRoot"] is True
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["backups"]["persistentVolumeClaim"]["claimName"]


def test_the_backup_needs_a_volume(chart_tools):
    done = _template(chart_tools, "--set", "backup.enabled=true")
    assert done.returncode != 0
    assert "backup.persistence" in done.stderr


@pytest.mark.parametrize("version", KUBERNETES_VERSIONS)
def test_kubeconform_accepts_every_environment(chart_tools, renders, version):
    for env in ENVIRONMENTS:
        done = chart_tools.kubeconform(renders[env], version)
        assert done.returncode == 0, f"{env}: {done.stdout}{done.stderr}"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_helm_lint_strict_per_environment(chart_tools, env):
    done = chart_tools.helm(
        ["lint", "--strict", "agentic-platform", "-f", _values(env), *INSTALL[env]]
    )
    assert done.returncode == 0, done.stdout + done.stderr


# ── runbooks ──────────────────────────────────────────────────────────────────


def test_the_promotion_runbook_covers_each_flow_and_the_kind_dry_run():
    text = RUNBOOK.read_text(encoding="utf-8")
    for heading in (
        "Framework upgrades",
        "Policy changes",
        "Agent and skill rollouts",
        "Promotion dry run",
    ):
        assert f"## {heading}" in text, heading
    for env in ENVIRONMENTS:
        assert f"environments/{env}.yaml" in text
    assert "ENVIRONMENT=staging" in text and "$env:ENVIRONMENT = 'staging'" in text
    assert "kind delete cluster --name b16-m3-envs" in text


def test_the_kind_smoke_installs_an_environment_and_runs_the_dry_run():
    kind = ROOT / "deploy" / "helm" / "kind"
    for name in ("smoke.sh", "smoke.ps1"):
        assert "-e ENVIRONMENT" in (kind / name).read_text(encoding="utf-8"), name
    inside = (kind / "inside.sh").read_text(encoding="utf-8")
    assert "environments/$ENVIRONMENT.yaml" in inside and "promotion.sh" in inside
    promotion = (kind / "promotion.sh").read_text(encoding="utf-8")
    assert "AGENTIC_CP_POLICY_BRANCH" in promotion and "--branch main" in promotion
    assert "<redacted>" in promotion, "the first admin's token never reaches the log"
    assert "create job backup-dry-run --from=" in promotion


def test_the_backup_cronjob_name_fits_for_a_long_release_name(chart_tools):
    # review: Kubernetes limits a CronJob's name to 52 characters
    done = chart_tools.helm(
        [
            "template",
            "r" * 53,
            "agentic-platform",
            "--namespace",
            NAMESPACE,
            "-f",
            _values("staging"),
        ]
    )
    assert done.returncode == 0, done.stderr
    assert len(_one(manifests(done.stdout), "CronJob", "backup")["metadata"]["name"]) <= 52


def test_a_failed_backup_leaves_no_partial_dump(renders):
    cron = _one(manifests(renders["staging"]), "CronJob", "backup")
    script = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "trap" in script and ".partial" in script


def test_prod_needs_the_ingress_host(chart_tools):
    done = _prod_without(chart_tools, "--set", "ingress.host=")
    assert done.returncode != 0
    assert "ingress.host" in done.stderr


# ── NetworkPolicy confirmation (owner decision on DF-805) ─────────────────────


def test_prod_refuses_a_network_policy_without_cni_confirmation(chart_tools):
    done = _prod_without(chart_tools, "--set", "networkPolicy.cniEnforces=false")
    assert done.returncode != 0
    assert "networkPolicy.cniEnforces" in done.stderr
    assert "ignored silently" in done.stderr


def test_prod_refuses_a_disabled_network_policy_without_a_reason(chart_tools):
    done = _prod_without(chart_tools, "--set", "networkPolicy.enabled=false")
    assert done.returncode != 0
    assert "networkPolicy.optOutReason" in done.stderr


@pytest.mark.parametrize("reason", ["", "   "], ids=["empty", "blank"])
def test_an_empty_opt_out_reason_is_refused(chart_tools, reason):
    done = _prod_without(
        chart_tools,
        "--set",
        "networkPolicy.enabled=false",
        "--set-string",
        f"networkPolicy.optOutReason={reason}",
    )
    assert done.returncode != 0
    assert "networkPolicy.optOutReason" in done.stderr


def test_prod_renders_with_the_cni_confirmed(renders):
    policy = _one(manifests(renders["prod"]), "NetworkPolicy", "api")
    assert "Egress" in policy["spec"]["policyTypes"]


def test_an_opt_out_with_a_reason_renders_and_is_echoed_in_the_notes(chart_tools):
    reason = "our CNI (flannel) does not enforce NetworkPolicy"
    done = _prod_without(
        chart_tools,
        "--set",
        "networkPolicy.enabled=false",
        "--set-string",
        f"networkPolicy.optOutReason={reason}",
    )
    assert done.returncode == 0, done.stderr
    assert not [o for o in manifests(done.stdout) if o["kind"] == "NetworkPolicy"]
    notes = chart_tools.helm(
        [
            "install",
            RELEASE,
            "agentic-platform",
            "--dry-run=client",
            "--namespace",
            NAMESPACE,
            "-f",
            _values("prod"),
            *INSTALL["prod"],
            "--set",
            "networkPolicy.enabled=false",
            "--set-string",
            f"networkPolicy.optOutReason={reason}",
        ]
    )
    assert notes.returncode == 0, notes.stderr
    assert reason in notes.stdout


def test_kind_staging_opts_out_honestly(renders):
    # kindnet does not enforce NetworkPolicy: staging says so instead of pretending
    values = YAML(typ="safe", pure=True).load(
        (CHART / "environments" / "staging.yaml").read_text("utf-8")
    )
    assert values["networkPolicy"]["enabled"] is False
    assert values["networkPolicy"]["optOutReason"] == "kind's CNI does not enforce NetworkPolicy"
    assert not [o for o in manifests(renders["staging"]) if o["kind"] == "NetworkPolicy"]


def test_staging_also_needs_the_confirmation_or_a_reason(chart_tools):
    done = _template(
        chart_tools, "-f", _values("staging"), "--set-string", "networkPolicy.optOutReason="
    )
    assert done.returncode != 0
    assert "networkPolicy.optOutReason" in done.stderr
