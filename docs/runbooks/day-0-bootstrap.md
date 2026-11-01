# Day-0 bootstrap: from an empty cluster to a working org (M3-05)

One ordered path for the platform team, from nothing to an org whose developers can run
`agentic`. Do it on **staging first**, then repeat it for prod. Each step says what you do,
what the script does, and how the step is checked. Steps you must do by hand (on your git
host, in Jira, in your secret store) say exactly what to click or set, and a later check
confirms the result.

The script is `scripts/bootstrap.py`, run with `uv run` from a checkout of the release tag,
the same on Windows, macOS and Linux. It only calls the framework's own commands (the
operator pod's `python -m agentic_cp admin ...`, `agentic policy check`,
`agentic rbac assign`, `agentic init`, `agentic run --dry-run`). Every step checks before it
changes anything, can be run again, and never prints a secret. `uv run python
scripts/bootstrap.py <step> --help` lists each step's options.

| Step | What | Who does it | Checked by |
|---|---|---|---|
| 0 | the control plane installed (Postgres, Secret, chart) | you, with Helm | `helm test` |
| 1 | the policy repo from the template | `bootstrap.py policy-repo`; you push and protect it | `agentic policy check`, `verify` |
| 2 | git access for the control plane (GitHub App or token) | you (git host), `bootstrap.py git-credentials` | `git ls-remote` in the operator pod, `verify` |
| 3 | SMTP and the public URL | you (values, Secret) | startup warnings, `verify` (invite sent) |
| 4 | the tenant and its first org admin | `bootstrap.py tenant` | `verify` (org-admin) |
| 5 | the CLI on the internal index | `scripts/release.py` | `verify --index-url` |
| 6 | people and roles | `bootstrap.py people` | `verify` (assignments, invites) |
| 7 | the Jira connector | each developer (and CI) | `agentic run --from-jira` |
| 8 | the day-0 checklist | `bootstrap.py verify` | exit 0 |
| 9 | the smoke run | `bootstrap.py smoke` | exit 0 |

What you need: a Kubernetes cluster and `kubectl`/`helm` that reach it, a Postgres
(managed or CloudNativePG), your git host (GitHub in the examples; GitLab and Bitbucket work
the same way), an SMTP relay, an internal Python package index, `uv` and `git` on your
machine, and this repository checked out at the release tag.

## 0. Install the control plane

Follow the chart's README (`deploy/helm/agentic-platform/README.md`, "Before you install"
and "Install") and [environments-and-promotion.md](environments-and-promotion.md) for the
environment's values: the image, Postgres with the owner role, the chart's Secret
(`database-url`, `owner-database-url`), then

```bash
helm upgrade --install agentic deploy/helm/agentic-platform --namespace agentic --create-namespace \
  -f deploy/helm/agentic-platform/environments/staging.yaml -f my-staging.yaml --wait --timeout 15m
helm test agentic --namespace agentic
```

The staging and prod values turn the nightly backup on. Its role reads every tenant row and
changes none; make it once with a superuser DSN (DF-891):

```bash
# on a trusted machine that reaches Postgres, with the release's control plane installed
# (uv run from this checkout); all four values from your secret store, never a command line
export AGENTIC_CP_DATABASE_URL=... AGENTIC_CP_OWNER_DATABASE_URL=...
export PG_ADMIN_DSN=... BACKUP_DB_PASSWORD=...
uv run python -m agentic_cp db backup-role --role agentic_backup
```

The superuser DSN is never put in the cluster: the operator pod has the owner DSN only,
where `db backup-role --role agentic_backup --check` confirms the role.

and put its DSN in the Secret under `backup-database-url`
([postgres-backup-restore.md](postgres-backup-restore.md)). Without a superuser, a DBA runs
the two statements in that runbook and `db backup-role --role agentic_backup --check`
confirms them.

The chart's **operator pod** (`operator.enabled`, zero replicas) is where every operator
command runs: same image, settings, policy branch and git config as the API, plus the owner
DSN. `bootstrap.py tenant` scales it up and back to zero for you. Anyone who may
`exec` into pods and `scale` Deployments in the namespace can reach the owner DSN through
it: give those rights to the platform team only (a Role bound to their group), or set
`operator.enabled: false` and enable it for a task.

## 1. The policy repo

```bash
uv run python scripts/bootstrap.py policy-repo --out ../agentic-org-policy \
  --repo-pattern 'github.com/acme/*' \
  --admins @acme/agentic-org-admins \
  --team checkout=@acme/checkout-leads --team search=@acme/search-leads \
  --model deep-reasoning=<gateway model> --model balanced=<gateway model> \
  --model fast=<gateway model> --model local=<local model> --model eval-judge=<model of another family> \
  --cli-version <release>
```

It copies `templates/policy-repo/` and the framework library (agents, pipelines, skills),
replaces the template's example handles, team and repository pattern with yours, names the
models you give (a tier you do not name keeps its `change-me` placeholder: name them all
before developers start), pins the release the policy-check workflow installs, commits on
`main` and makes the `staging` and `dev` branches. It then runs `agentic policy check`, the
same gate the pull requests run. Read `policy/org.yaml` (sandbox domains, required steps)
and `policy/teams/<team>.yaml` (`monthly_budget_usd`: 20 USD a developer a month to start)
before you push.

**By hand, on GitHub** (other hosts have the same settings under other names):

1. Create an empty private repository `acme/agentic-org-policy`, then push all three
   branches: `git remote add origin https://github.com/acme/agentic-org-policy.git` and
   `git push origin main staging dev`.
2. Settings, Branches: add a branch protection rule (or a ruleset) for each of `main`,
   `staging` and `dev`: *Require a pull request before merging*, *Require review from Code
   Owners*, *Require status checks to pass* with the checks **`check`** and **`budget`**
   (they appear after the first pull request), and *Do not allow bypassing*.
3. Settings, Secrets and variables, Actions: a **variable** `AGENTIC_INDEX_URL` with your
   index's simple URL (step 5). If the org admins in CODEOWNERS are a team (as above), a
   **secret** `ORG_READ_TOKEN`: a fine-grained token or GitHub App token with
   *Organization: Members (read)*. Without it
   the budget guard cannot tell who the org admins are and refuses every budget raise
   (DF-857: a team lead may only lower their own team's budget). The token needs nothing
   else: the reviews are read with the workflow's own token. Where GitHub offers it (an
   organisation ruleset with required workflows), require `policy-check` there too.

**Checked by:** the first pull request shows both checks; `bootstrap.py verify` (step 8)
shows the policy version the control plane activated.

## 2. Git access for the control plane

The control plane fetches the policy repo itself (and reads CODEOWNERS of the code repos).
It needs read access to them, through a per-tenant git config mounted from a Secret the
chart names in `gitConfigSecret`. Prefer a **GitHub App**: its tokens are short-lived and
limited to the repositories it is installed on.

**By hand, on GitHub (the App):**

1. Organization settings, Developer settings, GitHub Apps, *New GitHub App*: name
   `acme-agentic-control-plane`, no webhook (or the control plane's webhook URL if you use
   push-triggered syncs), Repository permissions **Contents: Read-only** and **Metadata:
   Read-only**, nothing else; *Only on this account*.
2. Generate a private key and store it in your secret manager (never on a laptop).
3. *Install App* on the organisation, **Only select repositories**: the policy repo and the
   code repositories agents work on. Note the App ID and the installation ID (the number at
   the end of the installation's settings URL).
4. Installation tokens expire after an hour, so let the External Secrets Operator mint them
   into the Secret (its `GithubAccessToken` generator; check the fields against your ESO
   version):

   ```yaml
   apiVersion: generators.external-secrets.io/v1alpha1
   kind: GithubAccessToken
   metadata: {name: agentic-github, namespace: agentic}
   spec:
     appID: "<app id>"
     installID: "<installation id>"
     auth:
       privateKey:
         secretRef: {name: github-app-key, key: private-key}   # from your secret store
   ---
   apiVersion: external-secrets.io/v1
   kind: ExternalSecret
   metadata: {name: agentic-git-token, namespace: agentic}
   spec:
     refreshInterval: 30m
     target: {name: agentic-git, creationPolicy: Merge}
     dataFrom:
       - sourceRef:
           generatorRef:
             apiVersion: generators.external-secrets.io/v1alpha1
             kind: GithubAccessToken
             name: agentic-github
         rewrite:
           - regexp: {source: "^token$", target: "acme.token"}
   ```

A **token** instead (a fine-grained personal access token of a bot account, *Contents:
read* on the same repositories) works the same way without the generator; rotate it before
it expires.

Then write the tenant's git config, which reads `<tenant>.token` from the same Secret (the
token is never in the config), and create the Secret:

```bash
uv run python scripts/bootstrap.py git-credentials --tenant acme --out ./git --host github.com
# with a token file (not with the App: ESO adds acme.token itself)
kubectl -n agentic create secret generic agentic-git --from-file=acme.gitconfig=./git/acme.gitconfig \
  --from-file=acme.token=<a file only you can read>
```

A git host with a private CA: add `--ca-file ca.pem` (the config then trusts it for that
host only). Set `gitConfigSecret: agentic-git` in your values and `helm upgrade`.

**Checked by** `git ls-remote` with the tenant's config in the operator pod (step 4 scales
it up), then the policy sync (step 8):

```bash
kubectl -n agentic exec deploy/agentic-agentic-platform-operator -- \
  env GIT_CONFIG_GLOBAL=/etc/agentic/git/acme.gitconfig git ls-remote --heads https://github.com/acme/agentic-org-policy.git
```

## 3. SMTP and the public URL

Invites (and gate waits and budget alerts) are mailed through your relay. In your values:

```yaml
publicUrl: https://agentic-staging.acme.example     # invite links point here
smtp:
  host: smtp.acme.example
  port: 587
  security: starttls        # tls for port 465; none only for a relay inside the cluster
  username: agentic         # the password goes in the chart's Secret as smtp-password
  from: agentic@acme.example
```

`helm upgrade`. The API and the worker print a `warning:` at startup for settings that
start but send nothing or send a secret in clear (DF-575, DF-578): a relay without a sender
or without `publicUrl`, a login over `security: none`, an `http://` gateway admin URL. Read
them: `kubectl -n agentic logs deploy/agentic-agentic-platform-worker | grep warning`.

**Checked by** step 6: `agentic rbac assign` says when an invite was not made (no public
URL) or is queued (no relay), and `verify --mail-to` checks the invite was sent. A mail the
relay refuses for good (a 5xx, an address it cannot carry) fails at once with its reason
(`agentic rbac mail`).

## 4. The tenant and its first org admin

```bash
uv run python scripts/bootstrap.py tenant --slug acme --name "Acme" \
  --admin-email platform-admin@acme.example --token-file ~/.agentic-bootstrap/acme-admin.token \
  --policy-repo https://github.com/acme/agentic-org-policy.git
```

It scales the operator pod to one, creates the tenant with its builtin roles and first org
admin (`admin tenant create`), writes the admin's token to the new file (only you may read
it: mode 0600, or on Windows an ACL for its owner only; it is never printed), points the tenant at the policy repo on the environment's branch
(`admin tenant set-policy-repo`; staging tracks `staging`), and scales the pod back to
zero. Run again, it skips an existing tenant. A lost token: run it again with
`--recover-token` and a new `--token-file` (`admin issue-token` for `--admin-email`). Keep the token in your secret store; the first admin
signs in with it (`agentic login --for control-plane --key-stdin`) and invites the rest.

## 5. The CLI on the internal index

[release.md](release.md): the one-time setup (signing key, the index's upload account) and
`scripts/release.py release`, which builds, signs, verifies, publishes and smoke-tests the
release from the index. The policy-check workflow (step 1) and every developer install from
that index:

```bash
uv tool install agentic-cli==<release> --index-url https://pypi.acme.internal/simple/
```

**Checked by** `verify --index-url <simple URL> --cli-version <release>` (both packages
the gate installs, `agentic-cli` and `agentic-core`).

## 6. People and roles

The builtin roles: `org-admin`, `team-lead`, `developer`, `approver`, `auditor`,
`exec-viewer`, `repo-maintainer` (derived from each repo's CODEOWNERS) and
`service-account`. Give the first team leads and developers theirs:

```bash
uv run python scripts/bootstrap.py people --api https://agentic-staging.acme.example --org acme \
  --token-file ~/.agentic-bootstrap/acme-admin.token \
  --assign lead@acme.example=team-lead@checkout --assign dev@acme.example=developer@checkout \
  --assign agentic-smoke@acme.example=developer@checkout
```

The last one is a smoke account for step 9: the smoke run redeems its invite, so the link
stops working; never use a real person's invite for it.

Each new person gets an invite mail. For many people use `agentic rbac import people.csv`
(a dry run until `--apply`). From here on team leads assign their own team's developers and
approvers.

## 7. The Jira connector

Jira is configured per developer, never on the server, so a token only reaches the site
its owner named. **Each developer**, in `~/.agentic/config.yaml`:

```yaml
jira:
  url: https://acme.atlassian.net        # or the Data Center URL
  user: dev@acme.example                 # Cloud: the account email; Data Center: leave out
```

then `agentic login --for jira` (a Cloud API token from id.atlassian.com, Security, API
tokens; or a Data Center personal access token), kept in the OS keyring. CI and service
accounts use `AGENTIC_JIRA_URL`, `AGENTIC_JIRA_USER` and `AGENTIC_JIRA_TOKEN`. **Checked
by** `agentic run feature-delivery --from-jira ACME-1 --dry-run`.

## 8. The day-0 checklist

```bash
uv run python scripts/bootstrap.py verify --api https://agentic-staging.acme.example \
  --token-file ~/.agentic-bootstrap/acme-admin.token --mail-to dev@acme.example \
  --index-url https://pypi.acme.internal/simple/ --cli-version <release>
```

PASS or FAIL for: the control plane ready with its database guarantees, the first org
admin, the policy repo synced (the active version, or the problems that rejected it), the
assignments, each invite sent, and both packages on the index; MANUAL for what only you can
see (branch protection, the App's installation, Jira). Exit 0 when nothing failed.

## 9. The smoke run

A developer's first session, without a model: the invite link from the developer's mail is
redeemed (the token stays in memory), then `agentic init` and `agentic run
feature-delivery --dry-run` in a scratch repository in org mode, so the CLI from the index,
the org policy and the control plane must agree (the run is registered as a dry run):

```bash
uv run python scripts/bootstrap.py smoke --api https://agentic-staging.acme.example --org acme \
  --team checkout --invite-link '<the link mailed to the smoke account>' \
  --repo-remote https://github.com/acme/any-repo.git --agentic "$(command -v agentic)"
```

Nothing is pushed. A full run with real models on staging's test repositories is the
acceptance suite (M3-25); a recorded replay is `agentic eval --mode replay`.

## Developers' first day

The invite mail says it: install the CLI from the index, `agentic login --invite '<link>'`,
`team:` in `~/.agentic/config.yaml`, `agentic init` in a repository. Two gaps remain
(DF-747): `agentic init --install-missing` puts tools in `~/.agentic/tools`, which people
add to `PATH` by hand with the command `init` prints for their shell; and on Windows a tool
is trusted because the org's `tool-pins.yaml` marks it `signed`, its Authenticode signature
is not checked on the laptop.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `tenant`: `kubectl rollout status` times out | the operator pod cannot start: `kubectl -n agentic describe pod -l app.kubernetes.io/component=operator` (image pull, the Secret's keys) |
| `verify`: no active policy, "nothing fetched yet" | the worker cannot fetch: step 2's `git ls-remote`, then the worker log (`policy sync`) |
| `verify`: no active policy with problems | the commit does not validate: `agentic policy check .` in the policy repo, fix, merge |
| "the tenant tracks the policy branch ..." in the worker log | the tenant was pointed at another branch: `bootstrap.py tenant` again (it sets the environment's branch) |
| `people`: "no invite: ... no public URL" | set `publicUrl`, `helm upgrade`, then `agentic rbac invite <email>` |
| `verify`: invite `pending` | the relay: the worker's `warning:` lines and `agentic rbac mail` (last error) |
| `smoke`: the dry run reports a sandbox problem | the machine has no sandbox the policy requires (`policy/org.yaml` `sandbox.required_for`); run the smoke where developers run agents |
| `helm upgrade`: `conflict with "kubectl" with subresource "scale" ... .spec.replicas` on the operator Deployment | the operator pod was left scaled up by `kubectl scale`, and Helm's server-side apply will not take the field back: `kubectl -n agentic scale deploy/agentic-agentic-platform-operator --replicas=0`, then upgrade again |
| a budget pull request fails `budget` | a raise needs an org admin as author or approver of the latest commit; see the check's log |

## Record of the local proof

`deploy/helm/kind/bootstrap_proof.py` runs this runbook on a local kind staging (owner
decision 114): the staging install of step 0 (the kind smoke), a local git host over https
with a private CA and a token (step 2's config, as for a real host), a fake SMTP relay
(Mailpit), a loopback package index for step 5 (`scripts/release.py`'s build, sign and
publish), then `bootstrap.py` steps 1, 2, 4, 6, 8 and 9:

```bash
uv run python deploy/helm/kind/bootstrap_proof.py --name b17-m3-05 --work <a scratch folder>
```

RECORD-PLACEHOLDER
