# Environments and promotion (M3-04)

The org deployment runs as three environments, each installed from the same umbrella chart
`deploy/helm/agentic-platform` with its own values file. A change reaches prod only after
it has run in staging. Installing and upgrading one environment is in
[upgrade.md](upgrade.md); backups and the restore drill are in
[postgres-backup-restore.md](postgres-backup-restore.md); cutting a release is in
[release.md](release.md).

## The three environments

| | dev | staging | prod |
|---|---|---|---|
| Purpose | the platform team builds and tests the framework | rehearse upgrades, policy changes and new agent or skill versions with real models on test repos | all 1000 users |
| Values file | `environments/dev.yaml` | `environments/staging.yaml` | `environments/prod.yaml` |
| Policy repo branch | `dev` | `staging` | `main` |
| Where | a shared dev cluster | a local kind cluster (owner decision 114); a cloud cluster later | the org's cluster |
| API | 1 replica | 2-3 (autoscaled) | 3-10 at 60 % CPU, 1 vCPU / 1 GB each |
| Secret | by hand or External Secrets | made by the kind smoke | External Secrets (or Sealed Secrets / CSI, declared) |
| TLS | optional | none on kind (port-forward) | required: ingress + cert-manager |
| Egress | open | open | NetworkPolicy allow-list (DF-805) |
| Backups | none | nightly CronJob, 7 days | nightly CronJob, 35 days, plus PITR |
| CLI version | pre-release | release candidate | released; `minCliVersion` enforced |
| TokenLean | its own, cheap tiers | its own, prod model mapping, low caps | the shared org instance |

Each values file is the environment's shape. The org's own names (registry, host names,
secret store keys, CIDRs, SMTP) go into a second values file of your own after it, so the
committed files never carry an org's details:

```bash
helm upgrade --install agentic deploy/helm/agentic-platform --namespace agentic \
  -f deploy/helm/agentic-platform/environments/prod.yaml -f my-prod.yaml \
  --set image.digest=sha256:<the release's digest> --wait
```

### The policy branch an environment tracks

`environment.name` sets `AGENTIC_CP_POLICY_BRANCH` on the API and the worker (dev ->
`dev`, staging -> `staging`, prod -> `main`; `environment.policyBranch` names another).
With it set, every tenant of that control plane tracks that branch:

- `admin tenant create` and `admin tenant set-policy-repo` use it when no branch is given,
  and refuse any other branch (exit 3);
- `PUT /v1/tenant/policy-source` uses it when the body has no `branch`, and refuses any
  other one with 422 `policy.branch_pinned`.

So a staging control plane can never apply `main`, and prod can never apply `staging`: a
policy change moves between environments only by a merge in the policy repo. Run operator
commands with the environment's `AGENTIC_CP_POLICY_BRANCH` too (copy it from the API
Deployment, as `deploy/helm/kind/promotion.sh` does). A tenant created before the pin keeps
its old branch until `set-policy-repo` is run for it: check `admin tenant list` (the
branch is after the `@`) after turning the pin on.

### What prod refuses

`environment.name: prod` does not render (so `helm install` stops before anything
changes) unless:

- the ingress serves TLS: `ingress.enabled`, `ingress.host`, `ingress.tlsSecretName`, and
  `publicUrl` starts with `https://`;
- a secret manager writes the chart's Secret: `externalSecrets.enabled`, or
  `secrets.externallyManaged: true` when Sealed Secrets or a CSI driver writes it. A Secret
  made by hand with `kubectl create secret` is not a prod secret;
- the image is pinned by digest: `image.digest`.

The chart never takes a secret value in any environment: secret settings in `config` are
refused, and the Secret is only referenced.

### External Secrets (optional, BYO store)

With the External Secrets Operator installed and a `ClusterSecretStore` (or `SecretStore`)
for your secret manager, set:

```yaml
externalSecrets:
  enabled: true
  secretStoreRef: {name: org-secret-store, kind: ClusterSecretStore}
  remoteRefs:
    databaseUrl: {key: agentic/prod/database-url}
    ownerDatabaseUrl: {key: agentic/prod/owner-database-url}
    metricsToken: {key: agentic/prod/metrics-token}          # optional
    smtpPassword: {key: agentic/prod/smtp-password}          # optional
    gatewayAdminKey: {key: agentic/prod/gateway-admin-key}   # optional
```

`property` picks one field of a JSON secret. The chart renders an `ExternalSecret` that
writes `secrets.existingSecret`. It is a `pre-install,pre-upgrade` hook that runs before
the migration Job, so the Job finds its Secret (its pod waits until the operator has
written it). The Secret is orphaned on purpose: re-creating the hook on each upgrade, or
`helm uninstall`, never deletes the Secret the running pods read. Helm does not remove
hook objects on uninstall either, so the `ExternalSecret` keeps refreshing the Secret
afterwards: delete both yourself when the environment is retired
(`kubectl -n agentic delete externalsecret,secret -l app.kubernetes.io/instance=agentic`
for the ExternalSecret, then the Secret by name). Helm waits for the migration Job, not for
the operator: on a first install the Job's pod waits (up to `migrations.activeDeadlineSeconds`,
15 minutes) until the operator has written the Secret.

### cert-manager TLS (optional)

With cert-manager and an issuer installed, `ingress.certManager.enabled` renders a
`Certificate` for `ingress.host` into `ingress.tlsSecretName` (ECDSA P-256, a new key at
each renewal):

```yaml
ingress:
  enabled: true
  className: nginx
  host: agentic.example.org
  tlsSecretName: agentic-tls
  certManager:
    enabled: true
    issuerRef: {name: org-issuer, kind: ClusterIssuer}
```

The alternative is cert-manager's ingress-shim: leave `certManager` off and put
`cert-manager.io/cluster-issuer: org-issuer` in `ingress.annotations`.

### Network policy in prod (DF-805)

`environments/prod.yaml` turns the NetworkPolicy on with an egress allow-list: Postgres,
public HTTPS and SMTP submission, never internal ranges or cloud metadata. It is the
network-level guard for tenant-set git URLs (DF-484, DF-503). It needs a CNI that enforces
policies (Calico, Cilium; kindnet and plain Flannel do not). Replace the Postgres CIDR,
and add private git hosts or a private TokenLean as their own `to` entries. If the
cluster's CNI cannot enforce policies, the guard is not there: say so in the
environment's record and track the application-level allow-list (DF-798).

## Framework upgrades

A release candidate goes to staging, is accepted there, then goes to prod.

1. **Cut the release candidate** ([release.md](release.md)) and push its image. Note its
   digest.
2. **Staging.** Upgrade staging with the candidate (on kind:
   `SKIP_BUILD=1 CP_IMAGE=<rc image> ENVIRONMENT=staging deploy/helm/kind/smoke.sh`,
   which also runs the migration hook twice and the restore runbook's stop and start). Then
   run `helm test`, the smoke run, the acceptance suite (M3-25) and the load test (M3-22)
   against staging, and a restore drill of staging's newest backup.
3. **Decide.** All green: promote the same digest. Anything red: fix forward with a new
   candidate; never promote a candidate that did not pass staging.
4. **Prod.** Take an extra logical backup if the release's migrations rewrite data, then
   `helm upgrade` prod with `environments/prod.yaml`, your prod values and
   `--set image.digest=<the same digest>` ([upgrade.md](upgrade.md)). `helm test`.
5. **Raise `minCliVersion`** in your prod values after the grace period
   ([upgrade.md](upgrade.md), "Raise the minimum CLI version").

Roll back with `helm rollback` ([upgrade.md](upgrade.md)); migrations expand before they
contract, so the previous release serves the migrated database.

## Policy changes

The policy repo has one long-lived branch per environment: `dev`, `staging` and `main`.
Protect `staging` and `main` (pull requests only, reviews from CODEOWNERS, status checks).

1. **Open a pull request to `staging`.** The policy repo's CI validates it.
2. **Merge.** The staging control plane fetches `staging` (poll or webhook), validates and
   applies it; `GET /v1/policy/active` shows the version in force and
   `GET /v1/policy/versions` each fetched version, with the problems that rejected one. A rejected version is never applied: the last good one stays.
3. **Smoke it on staging.** Run the smoke run (and, for a large change, the acceptance
   suite) with a staging tenant. Every run records the policy version it ran with.
4. **Open a pull request `staging` -> `main`.** Its diff is exactly what staging ran. Merge
   it; prod fetches `main` and applies it.
5. **Roll back** by reverting the merge on `main` (and on `staging`): the control plane
   applies the revert as a new version.

Hotfix on prod: still go through `staging` first, then `main`, as fast as the reviews
allow. Never push to `main` directly; a control plane pinned to `main` applies whatever
`main` holds.

`dev` is the platform team's: merge `main` back into `dev` after each promotion so dev does
not drift.

## Agent and skill rollouts

Agent, pipeline and skill versions live in the policy repo (`policy/rollout.yaml`), so they
promote like any policy change, plus an eval gate and a team-by-team rollout in prod.

1. **Publish** the new version in the policy repo (the previous one kept under
   `versions/`), through a pull request to `staging`.
2. **Eval gate (M1-63).** The pull request's CI runs the quality gate: the new version must
   not score below the previous one ([m1-64-quality-gate.md](m1-64-quality-gate.md)).
3. **Staging.** After the merge, run the new version on staging's test repos with real
   models.
4. **Prod, team by team.** Promote `staging` -> `main` with the version's `teams:` list in
   `policy/rollout.yaml` naming the pilot teams only. Every other team keeps `previous`.
   Widen the list in later pull requests (each `staging` first, then `main`) while the
   adoption and agent-quality dashboards stay healthy.
5. **Finish** by making the version current for everyone, and deprecate the old one
   (`deprecated:` with a `blocked_from` date). Roll back by removing a team from `teams:`.

## Promotion dry run

The dry run proves the promotion pieces on the local staging cluster: the staging values
install from the chart, the control plane tracks `staging` only, and the backup the restore
drill needs is taken. It needs Docker (about 4 GB of free memory); kind, kubectl and helm
run in the pinned tool container, so nothing native is installed.

macOS or Linux (bash):

```bash
KIND_CLUSTER=b16-m3-envs CP_IMAGE=agentic-control-plane:b16-m3-envs \
  ENVIRONMENT=staging deploy/helm/kind/smoke.sh
```

Windows (PowerShell 5.1 or 7):

```powershell
$env:KIND_CLUSTER = 'b16-m3-envs'; $env:CP_IMAGE = 'agentic-control-plane:b16-m3-envs'
$env:ENVIRONMENT = 'staging'
.\deploy\helm\kind\smoke.ps1
```

The smoke deletes the cluster at the end. With `KEEP=1` it stays; delete it with
`kind delete cluster --name b16-m3-envs` (or, without kind installed,
`docker rm -f b16-m3-envs-control-plane`).

What it does (`deploy/helm/kind/inside.sh` and `promotion.sh`):

1. builds the control-plane image, creates the kind cluster and a throw-away Postgres, and
   the chart's Secret with random passwords;
2. `helm install` with `environments/staging.yaml` (and the backup volume it names), then
   `helm test`;
3. checks that the API and the worker carry `AGENTIC_CP_POLICY_BRANCH=staging`;
4. in one-off operator pods with the same image and branch: creates the tenant `dry-run`
   without a branch (it tracks `staging`), tries to point it at `main` (refused, exit 3),
   points it at a policy repo on its own branch, and lists tenants;
5. runs the backup CronJob once and shows the dump it wrote;
6. `helm upgrade` twice (the migration hook on a migrated database, and the restore
   runbook's stop and start), each followed by `helm test`.

The first admin's token is redacted from the output. The policy repo URL in step 4 does
not resolve, so the worker logs a failed fetch: fetching and applying a real policy repo on
staging is the bootstrap's job (M3-05).

### Record of the dry run

RECORD_PLACEHOLDER

## Staging in the cloud

When staging moves from kind to a cloud cluster, install it as prod is installed: an
ingress controller, cert-manager, the External Secrets Operator and, for egress control, an
enforcing CNI. Use `environments/staging.yaml` plus a values file of your own that adds
what the kind cluster did not need:

```yaml
publicUrl: https://agentic-staging.example.org
ingress:
  enabled: true
  className: nginx
  host: agentic-staging.example.org
  tlsSecretName: agentic-staging-tls
  certManager: {enabled: true, issuerRef: {name: org-issuer, kind: ClusterIssuer}}
externalSecrets:
  enabled: true
  secretStoreRef: {name: org-secret-store, kind: ClusterSecretStore}
  remoteRefs:
    databaseUrl: {key: agentic/staging/database-url}
    ownerDatabaseUrl: {key: agentic/staging/owner-database-url}
networkPolicy:
  enabled: true
  ingressFrom:
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ingress-nginx}}
```

Then create the backup volume (`agentic-backups`), install, run `helm test`, bootstrap the
first tenant (M3-05), and repeat the promotion dry run's checks by hand:

```bash
kubectl -n agentic get deploy -l app.kubernetes.io/instance=agentic,app.kubernetes.io/component=api \
  -o jsonpath='{.items[0].spec.template.spec.containers[0].env[?(@.name=="AGENTIC_CP_POLICY_BRANCH")].value}'
kubectl -n agentic create job backup-now --from=cronjob/agentic-agentic-platform-backup
```
