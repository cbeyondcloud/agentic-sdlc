"""The umbrella chart `deploy/helm/agentic-platform` (M3-02), rendered and linted with the
pinned helm and validated with kubeconform (scripts/tests/helm_support.py says where they
run). Default values and the full example (`ci/full-values.yaml`) are both checked.

Review Focus: no secret values (every secret is a reference to `secrets.existingSecret`,
the owner DSN only in the migration Job and the retention CronJob), hardened pods, the
migration hook, one API replica set of two or more, and every catalog dashboard in a
Grafana ConfigMap."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from helm_support import (
    CHART,
    HELM_IMAGE,
    HELM_VERSION,
    KUBECONFORM_IMAGE,
    KUBECONFORM_VERSION,
    KUBERNETES_VERSIONS,
    ROOT,
    manifests,
    tools,
    values_file,
)
from ruamel.yaml import YAML

from agentic_cp.dashboards.catalog import GRAFANA_DIR, GRAFANA_EXECUTIVE_DIR

RELEASE = "agentic"
NAMESPACE = "agentic"
FULL = "agentic-platform/ci/full-values.yaml"
SECRET = "agentic-platform"
#: Settings that hold a secret: only ever a reference into the existing Secret.
SECRET_SETTINGS = {
    "AGENTIC_CP_DATABASE_URL",
    "AGENTIC_CP_OWNER_DATABASE_URL",
    "AGENTIC_CP_METRICS_TOKEN",
    "AGENTIC_CP_SMTP_PASSWORD",
    "AGENTIC_CP_GATEWAY_ADMIN_KEY",
}
DIGEST = re.compile(r"^[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


def _yaml(path: Path) -> Any:
    return YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def chart_tools():
    return tools()


def _template(chart_tools, *args: str, files: dict[str, str] | None = None):
    return chart_tools.helm(
        ["template", RELEASE, "agentic-platform", "--namespace", NAMESPACE, *args], files
    )


@pytest.fixture(scope="module")
def default_render(chart_tools) -> str:
    done = _template(chart_tools)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture(scope="module")
def full_render(chart_tools) -> str:
    done = _template(chart_tools, "-f", FULL)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _objects(text: str) -> list[dict[str, Any]]:
    return manifests(text)


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


def _pod_specs(objects) -> list[tuple[str, dict[str, Any]]]:
    specs = []
    for o in objects:
        if o["kind"] in ("Deployment", "Job"):
            specs.append((o["metadata"]["name"], o["spec"]["template"]["spec"]))
        elif o["kind"] == "CronJob":
            job = o["spec"]["jobTemplate"]["spec"]
            specs.append((o["metadata"]["name"], job["template"]["spec"]))
        elif o["kind"] == "Pod":
            specs.append((o["metadata"]["name"], o["spec"]))
    return specs


def _env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in container.get("env", [])}


# ── the chart's own metadata ──────────────────────────────────────────────────


def test_chart_metadata_and_the_optional_tokenlean_dependency():
    chart = _yaml(CHART / "Chart.yaml")
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "agentic-platform"
    assert chart["type"] == "application"
    (dependency,) = chart["dependencies"]
    assert dependency["name"] == "tokenlean-lite"
    assert dependency["condition"] == "tokenlean.enabled"
    assert dependency["repository"] == "file://charts/tokenlean-lite"
    assert _yaml(CHART / "values.yaml")["tokenlean"]["enabled"] is False
    stub = _yaml(CHART / "charts" / "tokenlean-lite" / "Chart.yaml")
    assert stub["name"] == "tokenlean-lite"


def test_the_tool_images_are_pinned_and_match_the_check_image():
    assert DIGEST.match(HELM_IMAGE) and f":{HELM_VERSION}@" in HELM_IMAGE
    assert DIGEST.match(KUBECONFORM_IMAGE) and f":v{KUBECONFORM_VERSION}@" in KUBECONFORM_IMAGE
    dockerfile = (ROOT / "scripts" / "linux-check.Dockerfile").read_text(encoding="utf-8")
    assert f"ARG HELM_VERSION={HELM_VERSION}\n" in dockerfile
    assert f"ARG KUBECONFORM_VERSION={KUBECONFORM_VERSION}\n" in dockerfile


def test_values_yaml_holds_no_secret_value():
    values = _yaml(CHART / "values.yaml")
    risky = re.compile(r"(password|token|secretkey|apikey|dsn|databaseurl)$", re.IGNORECASE)

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif path.startswith(".secrets.keys."):
            return  # key names inside the existing Secret, not values
        elif risky.search(path.rsplit(".", 1)[-1]):
            assert node in ("", None), f"{path} must not carry a value"

    walk(values, "")
    text = (CHART / "values.yaml").read_text(encoding="utf-8")
    assert not re.search(r"postgres(ql)?://[^\s:@/]+:[^\s@/]+@", text), "a DSN with a password"


def test_the_runbooks_exist_and_link_each_other():
    runbooks = ROOT / "docs" / "runbooks"
    upgrade = (runbooks / "upgrade.md").read_text(encoding="utf-8")
    restore = (runbooks / "postgres-backup-restore.md").read_text(encoding="utf-8")
    readme = (CHART / "README.md").read_text(encoding="utf-8")
    assert "(release.md)" in upgrade and "(postgres-backup-restore.md)" in upgrade
    assert "(upgrade.md)" in restore and "scripts/restore_drill.py" in restore
    assert "min_cli_version" in upgrade and "helm rollback" in upgrade
    for name in ("upgrade.md", "postgres-backup-restore.md"):
        assert f"(../../../docs/runbooks/{name})" in readme
    assert "(upgrade.md)" in (runbooks / "release.md").read_text(encoding="utf-8")


#: Stop writes: only the release's Deployments (review F3: never every Deployment).
SCALE_DOWN = (
    "kubectl -n agentic scale deploy -l app.kubernetes.io/instance=agentic,"
    "app.kubernetes.io/part-of=agentic-platform --replicas=0"
)
#: The HPA leaves the API at zero and helm upgrade never sets its replicas: scale it back.
SCALE_API_UP = (
    "kubectl -n agentic scale deploy -l app.kubernetes.io/instance=agentic,"
    "app.kubernetes.io/component=api --replicas=2"
)


def test_the_restore_runbook_scales_only_the_release_and_brings_the_api_back():
    restore = (ROOT / "docs" / "runbooks" / "postgres-backup-restore.md").read_text(
        encoding="utf-8"
    )
    assert not re.search(r"--all\b", restore), "kubectl --all scales every Deployment"
    assert SCALE_DOWN in restore and SCALE_API_UP in restore
    # the kind smoke runs the same commands (sh: the namespace is "$NS")
    inside = (ROOT / "deploy" / "helm" / "kind" / "inside.sh").read_text(encoding="utf-8")
    for command in (SCALE_DOWN, SCALE_API_UP):
        assert command.replace("-n agentic", '-n "$NS"') in inside, command


def test_the_kind_smoke_is_pinned_and_runs_in_containers():
    kind = ROOT / "deploy" / "helm" / "kind"
    tools = (kind / "tools.Dockerfile").read_text(encoding="utf-8")
    assert f"FROM {HELM_IMAGE}\n" in tools
    assert tools.count("sha256sum -c -") == 2  # kind and kubectl
    inside = (kind / "inside.sh").read_text(encoding="utf-8")
    assert re.search(r"kindest/node:v[0-9.]+@sha256:[0-9a-f]{64}", inside)
    assert "--from-literal" not in inside, "passwords go through files"
    for name in ("smoke.sh", "smoke.ps1"):
        text = (kind / name).read_text(encoding="utf-8")
        assert "/var/run/docker.sock" in text and "inside.sh" in text, name
    manifest = (kind / "postgres.yaml").read_text(encoding="utf-8")
    images = re.findall(r"image: (\S+)", manifest)
    assert images and all(DIGEST.match(image) for image in images)


# ── default values ────────────────────────────────────────────────────────────


def test_default_render_objects(default_render):
    objects = _objects(default_render)
    kinds = sorted(o["kind"] for o in objects)
    assert "Secret" not in kinds
    for kind in ("Deployment", "HorizontalPodAutoscaler", "PodDisruptionBudget", "Service"):
        assert kind in kinds
    assert kinds.count("Deployment") == 2
    assert "Ingress" not in kinds and "NetworkPolicy" not in kinds
    assert "ServiceMonitor" not in kinds


def test_the_api_runs_two_or_more_replicas_with_autoscaling_and_a_budget(default_render):
    objects = _objects(default_render)
    api = _one(objects, "Deployment", "api")
    assert "replicas" not in api["spec"], "the HPA owns the replica count"
    hpa = _one(objects, "HorizontalPodAutoscaler", "api")
    assert hpa["spec"]["scaleTargetRef"]["name"] == api["metadata"]["name"]
    assert hpa["spec"]["minReplicas"] >= 2
    assert hpa["spec"]["maxReplicas"] >= hpa["spec"]["minReplicas"]
    pdb = _one(objects, "PodDisruptionBudget", "api")
    assert pdb["spec"]["minAvailable"] == 1
    assert pdb["spec"]["selector"]["matchLabels"] == api["spec"]["selector"]["matchLabels"]


def test_without_autoscaling_the_api_keeps_two_replicas(chart_tools):
    done = _template(chart_tools, "--set", "api.autoscaling.enabled=false")
    assert done.returncode == 0, done.stderr
    objects = _objects(done.stdout)
    assert _one(objects, "Deployment", "api")["spec"]["replicas"] == 2
    assert not [o for o in objects if o["kind"] == "HorizontalPodAutoscaler"]


def test_api_probes_resources_and_command(default_render):
    api = _one(_objects(default_render), "Deployment", "api")
    (container,) = api["spec"]["template"]["spec"]["containers"]
    assert container["args"] == ["serve", "--host", "0.0.0.0", "--port", "8080"]
    assert container["ports"] == [{"name": "http", "containerPort": 8080, "protocol": "TCP"}]
    assert container["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": "http"}
    assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert container["startupProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    # sizing (master plan): 1 vCPU / 1 GB per replica
    assert container["resources"]["limits"] == {"cpu": "1", "memory": "1Gi"}
    assert container["resources"]["requests"] == {"cpu": "250m", "memory": "512Mi"}
    assert (
        container["image"] == "agentic-control-plane:" + _yaml(CHART / "Chart.yaml")["appVersion"]
    )


def test_every_pod_is_hardened(default_render):
    specs = _pod_specs(_objects(default_render))
    assert len(specs) >= 4  # api, worker, migration job, helm test pod
    for name, spec in specs:
        pod = spec["securityContext"]
        assert pod["runAsNonRoot"] is True, name
        assert pod["runAsUser"] == 10001 and pod["runAsGroup"] == 10001, name
        assert pod["fsGroup"] == 10001, name
        assert pod["seccompProfile"] == {"type": "RuntimeDefault"}, name
        assert spec["automountServiceAccountToken"] is False, name
        # DF-415: Kubernetes service-link variables (AGENTIC_CP_SERVICE_HOST...) would be
        # refused as unknown settings
        assert spec["enableServiceLinks"] is False, name
        for container in spec["containers"]:
            security = container["securityContext"]
            assert security["readOnlyRootFilesystem"] is True, name
            assert security["allowPrivilegeEscalation"] is False, name
            assert security["capabilities"] == {"drop": ["ALL"]}, name
            mounts = {m["mountPath"]: m["name"] for m in container.get("volumeMounts", [])}
            assert "/tmp" in mounts, f"{name}: a writable /tmp"
        volumes = {v["name"]: v for v in spec.get("volumes", [])}
        assert "emptyDir" in volumes["tmp"], name


def test_the_worker_is_one_leader_with_standbys(default_render):
    worker = _one(_objects(default_render), "Deployment", "worker")
    assert worker["spec"]["replicas"] == 2  # one leader (advisory lock), one standby
    (container,) = worker["spec"]["template"]["spec"]["containers"]
    assert container["args"] == ["worker"]
    assert "ports" not in container
    assert container["resources"]["limits"]["memory"] == "1Gi"


def test_the_migration_job_is_a_pre_install_and_pre_upgrade_hook(default_render):
    job = _one(_objects(default_render), "Job", "migrate")
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert annotations["helm.sh/hook-weight"] == "-5"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    spec = job["spec"]
    assert spec["backoffLimit"] >= 1 and spec["activeDeadlineSeconds"] > 0
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    (container,) = pod["containers"]
    assert container["command"] == ["sh", "-c"]
    assert container["args"] == [
        "python -m agentic_cp migrate && python -m agentic_cp db grant-runtime"
    ]
    env = _env(container)
    assert env["AGENTIC_CP_OWNER_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": SECRET,
        "key": "owner-database-url",
    }


def test_no_secret_value_is_rendered_and_the_owner_dsn_reaches_only_the_jobs(full_render):
    objects = _objects(full_render)
    assert not [o for o in objects if o["kind"] == "Secret"]
    for name, spec in _pod_specs(objects):
        for container in spec["containers"]:
            env = _env(container)
            for setting in SECRET_SETTINGS & env.keys():
                assert "value" not in env[setting], f"{name}: {setting} has a literal value"
                assert env[setting]["valueFrom"]["secretKeyRef"]["name"] == SECRET
            # the migration Job and the retention CronJob (M3-06) only
            is_job = name.endswith(("-migrate", "-retention"))
            assert ("AGENTIC_CP_OWNER_DATABASE_URL" in env) == is_job, name
    assert "postgresql://" not in full_render


def test_the_retention_cronjob_runs_daily_one_at_a_time(default_render, chart_tools):
    """M3-06: a daily `retention run` with the owner DSN; runs never overlap."""
    cron = _one(_objects(default_render), "CronJob", "retention")
    spec = cron["spec"]
    assert spec["schedule"] == "17 3 * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    (container,) = pod["containers"]
    assert container["args"] == ["retention", "run"]
    assert _env(container)["AGENTIC_CP_OWNER_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": SECRET,
        "key": "owner-database-url",
    }
    off = _template(chart_tools, "--set", "retention.enabled=false")
    assert off.returncode == 0, off.stderr
    assert not [o for o in _objects(off.stdout) if o["kind"] == "CronJob"]
    dry = _template(chart_tools, "--set", "retention.dryRun=true")
    (container,) = _one(_objects(dry.stdout), "CronJob", "retention")["spec"]["jobTemplate"][
        "spec"
    ]["template"]["spec"]["containers"]
    assert container["args"] == ["retention", "run", "--dry-run"]


def test_a_secret_setting_in_config_is_refused(chart_tools):
    done = _template(chart_tools, "--set", "config.AGENTIC_CP_SMTP_PASSWORD=x")
    assert done.returncode != 0
    assert "secrets.existingSecret" in done.stderr


@pytest.mark.parametrize(
    "name",
    ["AGENTIC_CP_Smtp_Password", "AGENTIC_CP_database_url", "agentic_cp_metrics_token"],
)
def test_a_secret_setting_in_config_is_refused_in_any_case(chart_tools, name):
    # review F1: the control plane lower-cases a setting's name, so the guard must too
    done = _template(chart_tools, "--set", f"config.{name}=postgresql://evil.example/x")
    assert done.returncode != 0, f"config.{name} was rendered"
    assert "secrets.existingSecret" in done.stderr


def test_a_config_setting_must_be_in_upper_case(chart_tools):
    done = _template(chart_tools, "--set", "config.AGENTIC_CP_policy_poll_seconds=60")
    assert done.returncode != 0
    assert "AGENTIC_CP_POLICY_POLL_SECONDS" in done.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [("AGENTIC_CP_PUBLIC_URL", "publicUrl"), ("AGENTIC_CP_Gateway_Url", "tokenlean.url")],
)
def test_a_config_setting_the_chart_sets_itself_is_refused(chart_tools, name, value):
    # review F1: a duplicate env name is refused by server-side apply (Helm 4, Argo CD, Flux)
    done = _template(
        chart_tools,
        "--set",
        f"config.{name}=http://x.example",
        "--set",
        "publicUrl=https://y.example.org",
    )
    assert done.returncode != 0, f"config.{name} was rendered"
    assert value in done.stderr


def test_no_container_has_a_duplicate_env_name(full_render):
    for name, spec in _pod_specs(_objects(full_render)):
        for container in spec["containers"]:
            names = [item["name"] for item in container.get("env", [])]
            assert len(names) == len(set(names)), f"{name}: {names}"


def test_the_service_points_at_the_api(default_render):
    objects = _objects(default_render)
    service = _one(objects, "Service", "api")
    api = _one(objects, "Deployment", "api")
    assert service["spec"]["selector"] == api["spec"]["selector"]["matchLabels"]
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}
    ]


def test_every_catalog_dashboard_is_in_a_grafana_configmap(default_render):
    objects = _objects(default_render)
    dashboards = [
        o
        for o in objects
        if o["kind"] == "ConfigMap" and o["metadata"]["labels"].get("grafana_dashboard") == "1"
    ]
    rendered = {key: o for o in dashboards for key in o["data"]}
    # each catalog folder lands in its own Grafana folder: Executive ROI shows each person's
    # cost and must never be provisioned into the shared one (M3-01 review F3)
    folders = {GRAFANA_DIR: "Agentic", GRAFANA_EXECUTIVE_DIR: "Agentic Executive"}
    catalog = {p.name: (p, folders[d]) for d in folders for p in d.glob("*.json")}
    assert any(folder == "Agentic Executive" for _, folder in catalog.values())
    assert set(rendered) == set(catalog) and len(dashboards) == len(catalog)
    for name, (path, folder) in catalog.items():
        o = rendered[name]
        text = path.read_bytes().decode("utf-8")
        assert o["data"][name].replace("\r\n", "\n") == text.replace("\r\n", "\n"), name
        assert o["metadata"]["annotations"]["grafana_folder"] == folder, name
        assert o["metadata"]["name"] == f"{RELEASE}-agentic-platform-dash-{name[:-5]}"  # DF-808


@pytest.mark.parametrize("folder", ["", "Agentic"])
def test_an_executive_folder_that_is_empty_or_shared_is_refused(chart_tools, folder):
    # Executive ROI shows each person's cost (M3-01 review F3): an empty annotation lands it
    # in Grafana's General folder, the shared one in "Agentic"
    done = _template(chart_tools, "--set-string", f"grafana.dashboards.executiveFolder={folder}")
    assert done.returncode != 0
    assert "grafana.dashboards.executiveFolder" in done.stderr


def test_the_chart_copy_of_the_dashboards_matches_the_catalog():
    for source, chart_folder in (
        (GRAFANA_DIR, "dashboards"),
        (GRAFANA_EXECUTIVE_DIR, "dashboards-executive"),
    ):
        copy = {p.name: p.read_bytes() for p in (CHART / chart_folder).glob("*.json")}
        catalog = {p.name: p.read_bytes() for p in source.glob("*.json")}
        assert catalog, source
        assert copy.keys() == catalog.keys(), "run `uv run python -m agentic_cp dashboards --write`"
        for name in catalog:
            assert copy[name].replace(b"\r\n", b"\n") == catalog[name].replace(b"\r\n", b"\n")


# ── full values ───────────────────────────────────────────────────────────────


def test_full_values_add_ingress_monitoring_policy_and_urls(full_render):
    objects = _objects(full_render)
    ingress = _one(objects, "Ingress", "api")
    rule = ingress["spec"]["rules"][0]
    assert rule["host"] == "agentic.example.org"
    assert ingress["spec"]["tls"][0]["secretName"] == "agentic-tls"
    monitor = _one(objects, "ServiceMonitor", "api")
    (endpoint,) = monitor["spec"]["endpoints"]
    assert endpoint["path"] == "/metrics"
    assert endpoint["authorization"]["credentials"] == {"name": SECRET, "key": "metrics-token"}
    policy = _one(objects, "NetworkPolicy", "api")
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    api = _one(objects, "Deployment", "api")
    env = _env(api["spec"]["template"]["spec"]["containers"][0])
    assert env["AGENTIC_CP_GATEWAY_URL"]["value"] == "https://tokenlean.example.org"
    assert env["AGENTIC_CP_PUBLIC_URL"]["value"] == "https://agentic.example.org"
    assert env["AGENTIC_CP_SMTP_HOST"]["value"] == "smtp.example.org"
    assert env["AGENTIC_CP_MIN_CLI_VERSION"]["value"] == "0.1.0"
    datasource = [
        o
        for o in objects
        if o["kind"] == "ConfigMap" and o["metadata"]["labels"].get("grafana_datasource") == "1"
    ]
    (text,) = datasource[0]["data"].values()
    assert "uid: agentic-postgres" in text
    assert "password: ${AGENTIC_DASH_READER_PASSWORD}" in text


def _release_peer() -> dict[str, Any]:
    return {
        "podSelector": {
            "matchLabels": {
                "app.kubernetes.io/name": "agentic-platform",
                "app.kubernetes.io/instance": RELEASE,
            }
        }
    }


def test_the_network_policy_lets_the_release_reach_the_api_and_names_its_peers(full_render):
    # review F4: the helm test pod, the ingress controller and Prometheus all reach the API
    policy = _one(_objects(full_render), "NetworkPolicy", "api")
    (rule,) = policy["spec"]["ingress"]
    assert rule["ports"] == [{"port": "http", "protocol": "TCP"}]
    peers = rule["from"]
    assert _release_peer() in peers, "the release's own pods (helm test)"
    namespaces = [
        p["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        for p in peers
        if "namespaceSelector" in p
    ]
    assert namespaces == ["ingress-nginx", "monitoring"]
    api = {**_release_peer()["podSelector"]["matchLabels"], "app.kubernetes.io/component": "api"}
    to_api = {
        "to": [{"podSelector": {"matchLabels": api}}],
        "ports": [{"port": "http", "protocol": "TCP"}],
    }
    assert to_api in policy["spec"]["egress"], "egress from the release's pods to the API"


def test_the_example_egress_list_keeps_internal_ranges_out():
    text = (CHART / "ci" / "full-values.yaml").read_text(encoding="utf-8")
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
        assert f"- {cidr}\n" in text, cidr
    assert "- 100.64.0.0/10\n" in text


@pytest.mark.parametrize(
    ("switch", "needed"),
    [
        (["ingress.enabled=true", "ingress.host=agentic.example.org"], "networkPolicy.ingressFrom"),
        (["metrics.serviceMonitor.enabled=true"], "networkPolicy.metricsFrom"),
        (["metrics.scrapeAnnotations=true"], "networkPolicy.metricsFrom"),
    ],
    ids=["ingress", "servicemonitor", "annotations"],
)
def test_a_network_policy_without_the_needed_peers_is_refused(chart_tools, switch, needed):
    args = ["--set", "networkPolicy.enabled=true"]
    for item in switch:
        args += ["--set", item]
    done = _template(chart_tools, *args)
    assert done.returncode != 0, "rendered a policy that blocks its own callers"
    assert needed in done.stderr


def test_the_tokenlean_subchart_is_a_placeholder_until_df_797(chart_tools):
    # the TokenLean LITE chart is not published yet (DF-797): BYO TokenLean until then
    done = _template(chart_tools, "--set", "tokenlean.enabled=true")
    assert done.returncode != 0
    assert "DF-797" in done.stderr and "tokenlean.url" in done.stderr


# ── lint and schema validation ────────────────────────────────────────────────


@pytest.mark.parametrize("extra", [[], ["-f", FULL]], ids=["default", "full"])
def test_helm_lint_strict(chart_tools, extra):
    done = chart_tools.helm(["lint", "--strict", "agentic-platform", *extra])
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("version", KUBERNETES_VERSIONS)
def test_kubeconform_accepts_the_manifests(chart_tools, default_render, full_render, version):
    for text in (default_render, full_render):
        done = chart_tools.kubeconform(text, version)
        assert done.returncode == 0, done.stdout + done.stderr


def test_values_files_reach_the_container(chart_tools):
    # the runner's own contract: a values file passed by name is used
    files = {"extra.yaml": values_file({"worker": {"replicas": 3}})}
    done = _template(chart_tools, "-f", "extra.yaml", files=files)
    assert done.returncode == 0, done.stderr
    assert _one(_objects(done.stdout), "Deployment", "worker")["spec"]["replicas"] == 3


# ── M3-04 review follow-ups ───────────────────────────────────────────────────

#: Secrets only the API and the worker need (DF-807): never in the owner-DSN Job.
LONG_RUNNING_SECRETS = {
    "AGENTIC_CP_METRICS_TOKEN",
    "AGENTIC_CP_SMTP_PASSWORD",
    "AGENTIC_CP_GATEWAY_ADMIN_KEY",
}


def test_the_migration_job_gets_only_the_secrets_it_needs(full_render):
    # DF-807: the one pod holding the owner DSN gets no metrics, SMTP or gateway secret
    objects = _objects(full_render)
    job = _env(_one(objects, "Job", "migrate")["spec"]["template"]["spec"]["containers"][0])
    assert not LONG_RUNNING_SECRETS & job.keys()
    assert {"AGENTIC_CP_DATABASE_URL", "AGENTIC_CP_OWNER_DATABASE_URL"} <= job.keys()
    for component in ("api", "worker"):
        deployment = _one(objects, "Deployment", component)
        env = _env(deployment["spec"]["template"]["spec"]["containers"][0])
        assert LONG_RUNNING_SECRETS <= env.keys(), component


def test_dashboard_configmap_names_stay_unique_for_a_long_release_name(chart_tools):
    # DF-808: a fullname of 51+ characters used to truncate every name to the same prefix
    release = "a" * 53
    done = chart_tools.helm(["template", release, "agentic-platform", "--namespace", NAMESPACE])
    assert done.returncode == 0, done.stderr
    names = [
        o["metadata"]["name"]
        for o in _objects(done.stdout)
        if o["kind"] == "ConfigMap" and "grafana_dashboard" in o["metadata"]["labels"]
    ]
    expected = len(list(GRAFANA_DIR.glob("*.json"))) + len(
        list(GRAFANA_EXECUTIVE_DIR.glob("*.json"))
    )
    assert len(names) == expected
    assert len(set(names)) == len(names), names
    assert all(len(name) <= 63 for name in names)


def test_two_long_release_names_with_one_prefix_get_different_dashboard_names(chart_tools):
    # review of DF-808: cutting the fullname to 40 characters alone would collide
    def names(release: str) -> set[str]:
        done = chart_tools.helm(["template", release, "agentic-platform", "--namespace", NAMESPACE])
        assert done.returncode == 0, done.stderr
        return {
            o["metadata"]["name"]
            for o in _objects(done.stdout)
            if o["kind"] == "ConfigMap" and "grafana_dashboard" in o["metadata"]["labels"]
        }

    assert not names("p" * 45 + "-blue") & names("p" * 45 + "-green")
