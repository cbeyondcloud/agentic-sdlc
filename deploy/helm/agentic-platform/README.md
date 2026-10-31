# agentic-platform Helm chart (M3-02)

The framework's server side on any Kubernetes cluster (1.29 or later, any cloud or on
premises): the control-plane API, its background worker, database migrations, and the
Grafana dashboards. Postgres, Grafana and TokenLean are bring-your-own.

| Part | Object | Notes |
|---|---|---|
| API | Deployment, Service, HPA (2-6), PodDisruptionBudget | probes on `/healthz` (liveness) and `/readyz` (readiness) |
| Worker | Deployment (2 replicas) | one leader at a time (a Postgres lock), the other waits |
| Migrations | Job, `pre-install,pre-upgrade` hook | `migrate`, then `db grant-runtime` |
| Dashboards | one ConfigMap per dashboard, label `grafana_dashboard: "1"` | for Grafana's dashboard sidecar |
| Datasource | ConfigMap, label `grafana_datasource: "1"` (optional) | password from Grafana's environment |
| Optional | Ingress, ServiceMonitor, NetworkPolicy | off by default |
| Optional (M3-04) | ExternalSecret, cert-manager Certificate, backup CronJob | off by default; prod values turn them on |
| TokenLean | sub-chart `tokenlean-lite` (`tokenlean.enabled`, off) | a placeholder until the TokenLean chart is published (DF-797): run TokenLean yourself |

Every pod runs as user 10001 with a read-only root file system, no privilege escalation,
no Linux capabilities, the `RuntimeDefault` seccomp profile, no service-account token and
no Kubernetes service-link variables.

## Before you install

1. **The image.** Build `control-plane/Dockerfile` from the repository root and push it to
   your registry. Set `image.repository` and, preferably, `image.digest`.

   ```bash
   docker build -f control-plane/Dockerfile -t registry.example.org/agentic/agentic-control-plane:0.1.0 .
   ```

2. **Postgres** (17 recommended): a database `agentic`, an owner role with `CREATEROLE`
   (it owns the schema and runs migrations), and a runtime role name for the API (the
   migration Job creates it). HA and backups: [postgres-backup-restore.md](../../../docs/runbooks/postgres-backup-restore.md).
   For the dashboards' TokenLean link, a superuser runs once:
   `CREATE EXTENSION postgres_fdw; GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO <owner>;`

3. **The Secret.** One Secret in the release namespace, named by
   `secrets.existingSecret` (default `agentic-platform`). Make it with External Secrets,
   Sealed Secrets or by hand. The chart only reads it; it never holds a secret itself.

   | Key | Needed by | Value |
   |---|---|---|
   | `database-url` | API, worker, migrations | `postgresql://<runtime role>:<password>@<host>:5432/agentic` |
   | `owner-database-url` | migration Job only | `postgresql://<owner role>:<password>@<host>:5432/agentic` |
   | `metrics-token` | optional | 16+ characters; Prometheus sends it as a bearer token |
   | `smtp-password` | optional | the SMTP relay's password |
   | `gateway-admin-key` | optional | TokenLean's admin key |
   | `backup-database-url` | backup CronJob only (`backup.enabled`) | `postgresql://agentic_backup:<password>@<host>:5432/agentic`, a role with `BYPASSRLS` and `pg_read_all_data` |

   ```bash
   kubectl -n agentic create secret generic agentic-platform \
     --from-literal=database-url="$CP_DATABASE_URL" \
     --from-literal=owner-database-url="$CP_OWNER_DATABASE_URL"
   ```

## Environments (M3-04)

`environments/dev.yaml`, `environments/staging.yaml` and `environments/prod.yaml` give each
environment its shape and the policy repo branch it tracks (`dev`, `staging`, `main`, set
as `AGENTIC_CP_POLICY_BRANCH`). `environment.name: prod` refuses to render without ingress
TLS, a managed Secret (`externalSecrets.enabled` or `secrets.externallyManaged`) and
`image.digest`. staging and prod also refuse to render unless the NetworkPolicy is on with
`networkPolicy.cniEnforces: true` (the CNI enforces it; an unenforced policy is ignored
silently) or is off with a `networkPolicy.optOutReason`, which NOTES prints. Staging is a local kind cluster for now (`ENVIRONMENT=staging
deploy/helm/kind/smoke.sh`). Values, External Secrets, cert-manager and the promotion flows:
[environments-and-promotion.md](../../../docs/runbooks/environments-and-promotion.md).

## Install

```bash
helm upgrade --install agentic deploy/helm/agentic-platform \
  --namespace agentic --create-namespace --values my-values.yaml --wait
helm test agentic --namespace agentic
```

`ci/full-values.yaml` shows every option switched on. The main values:

| Value | Default | Meaning |
|---|---|---|
| `image.repository`, `image.digest` | `agentic-control-plane`, empty | the control-plane image |
| `secrets.existingSecret` | `agentic-platform` | the Secret above; `secrets.keys.*` rename its keys |
| `publicUrl` | empty | where people reach the API (invite links) |
| `minCliVersion` | empty | the deployment's minimum CLI version ([upgrade.md](../../../docs/runbooks/upgrade.md)) |
| `tokenlean.url`, `tokenlean.adminUrl` | empty | your TokenLean (BYO) |
| `smtp.*` | no relay | the org's SMTP relay; mail stays queued without one |
| `config` | `{}` | any other `AGENTIC_CP_*` setting; secret ones are refused |
| `gitConfigSecret` | empty | a Secret with `<tenant>.gitconfig` files for private repositories |
| `api.autoscaling.*` | 2-6 replicas at 70% CPU | `minReplicas` must be 2 or more |
| `api.resources` | 250m-1 CPU, 512Mi-1Gi | the master plan's sizing; revisit after the load test |
| `worker.replicas` | 2 | one leader, one standby |
| `migrations.grantRuntime` | true | turn off if you manage the runtime role yourself |
| `ingress.*` | off | host, class, TLS Secret; `ingress.certManager.*` makes a Certificate |
| `environment.name`, `environment.policyBranch` | empty | dev, staging or prod and the policy branch it tracks |
| `externalSecrets.*` | off | an ExternalSecret writing the Secret from your store (BYO) |
| `backup.*` | off | the nightly `pg_dump` CronJob into an existing volume |
| `metrics.serviceMonitor.enabled` | false | a prometheus-operator ServiceMonitor with the metrics token |
| `networkPolicy.*` | off | allowed callers and an egress allow-list (below) |
| `grafana.dashboards.*`, `grafana.datasource.*` | dashboards on, datasource off | sidecar labels, folders, namespace |

## First tenant

The operator commands need the owner role, which the long-running pods do not have. Run
them in a one-off pod with the same image and both DSNs (for example a copy of the
migration Job, or `kubectl run` with `envFrom` the Secret mapped to
`AGENTIC_CP_DATABASE_URL` and `AGENTIC_CP_OWNER_DATABASE_URL`):

```bash
python -m agentic_cp admin tenant create --slug acme --name "Acme" --admin-email admin@acme.example
```

The first admin's token is printed once. M3-05 turns this into a bootstrap runbook.

## Grafana (bring your own)

With the Grafana chart, turn on its sidecars (`sidecar.dashboards.enabled`,
`sidecar.datasources.enabled`, `sidecar.dashboards.folderAnnotation: grafana_folder`) and
let them search this namespace, or set `grafana.dashboards.namespace` to Grafana's. For
the datasource set `grafana.datasource.enabled`, `host` and `user` (a tenant's reader
role, made with `python -m agentic_cp db dashboard-role`), and give Grafana the reader's
password as the environment variable `AGENTIC_DASH_READER_PASSWORD` (the Grafana chart's
`envFromSecret`). TokenLean's own dashboards come with TokenLean.

Executive ROI shows each person's cost, so it goes to its own Grafana folder,
`grafana.dashboards.executiveFolder` (default *Agentic Executive*); the other dashboards go
to `grafana.dashboards.folder` (*Agentic*). Limit the executive folder's permissions to the
org viewers in Grafana.

A new dashboard in the catalog reaches the chart with
`uv run python -m agentic_cp dashboards --write`, which refreshes `dashboards/` and
`dashboards-executive/` here;
`--check` and the chart tests fail while the copy differs.

## Network policy

`networkPolicy.enabled` limits who reaches the API. It works only when your CNI enforces
network policies (Calico and Cilium do; kindnet and plain Flannel do not). The API port is
open to:

- the release's own pods, so `helm test` always works;
- `ingressFrom`, default: the release namespace. With `ingress.enabled`, name the ingress
  controller here (for example its namespace); the chart refuses to render without it;
- `metricsFrom`: Prometheus, for example the monitoring namespace. It is required with
  the ServiceMonitor or the scrape annotations.

With an `egress` list the policy also limits where every pod may connect: DNS, the API
(for the release's own pods) and the list. List Postgres, TokenLean, your git hosts and
the SMTP relay. When you allow the public internet on a port, keep internal ranges out
with `except`: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16` (cloud
metadata) and `100.64.0.0/10` (`ci/full-values.yaml` shows it). This is also the
network-level guard for tenant-set git URLs: a tenant admin cannot make the control plane
reach an internal host outside the list (DF-484).

## Retention

`retention.enabled` (on by default) runs `python -m agentic_cp retention run` as a daily
CronJob (`retention.schedule`, `17 3 * * *`; `retention.timeZone`). It deletes what each
tenant's `retention.yaml` says has expired and writes one `retention.pruned` audit event
per tenant (M3-06, OR-01). It gets the owner DSN, like the migration Job; runs never overlap
(`concurrencyPolicy: Forbid` and an advisory lock). `retention.dryRun: true` only logs what
it would delete. Backups are kept 35 days by default (`retention.yaml` `days.backups`): set
that in your backup tooling. See [docs/runbooks/retention.md](../../../docs/runbooks/retention.md).

## Upgrades, rollback, backups

[docs/runbooks/upgrade.md](../../../docs/runbooks/upgrade.md) and
[docs/runbooks/postgres-backup-restore.md](../../../docs/runbooks/postgres-backup-restore.md).
The chart's `version` is the release version in SemVer form (`0.4.0-rc.1` for `0.4.0rc1`);
`scripts/release.py version set` moves it with the packages.

## Local cluster smoke (kind)

`deploy/helm/kind/smoke.sh` (or `smoke.ps1`) builds the control-plane image, creates a
throw-away kind cluster, starts a Postgres in it, installs the chart, runs `helm test`,
upgrades it (the migration hook again) and tests again. kind, kubectl and helm run in a
pinned tool container that drives your Docker through its socket, so nothing native is
installed. It needs Docker and about 4 GB of free memory, and takes a few minutes.
`KEEP=1` keeps the cluster; `SKIP_BUILD=1 CP_IMAGE=<image>` reuses an image;
`ENVIRONMENT=staging` installs with the staging values and runs the promotion dry run.

## Tests

`scripts/tests/test_helm_chart.py` renders the chart with helm 4.3.1 for default and full
values, runs `helm lint --strict`, and validates the manifests with kubeconform 0.8.0
against Kubernetes 1.30 and 1.36. helm and kubeconform run only in Linux containers: the
check image (`scripts/check.py --linux`) has them, and on Windows or macOS the tests run
them in pinned images through docker or podman. Without either, they skip and say why.
