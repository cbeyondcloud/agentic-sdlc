# User and org settings people ask about

## Your own settings: `~/.agentic/config.yaml`

`agentic` reads one user config file: `~/.agentic/config.yaml` (`AGENTIC_HOME` instead of
`~/.agentic` when set), on Windows, macOS and Linux alike. Tokens are never written
there. `agentic login --for <name>` keeps them in the OS keyring, and CI passes them as
`AGENTIC_<CREDENTIAL>` environment variables.

### Where keys are kept (`credential_store:`)

The OS keyring (Windows Credential Manager, macOS Keychain, a Secret Service on Linux)
always comes first. A machine with no keyring at all - WSL2 or a Linux box without a
Secret Service - keeps keys in `~/.agentic/credentials.json` instead: readable only by
you (0600; agentic refuses the file when others can read it), not encrypted. `agentic
login` warns when it uses the file and `agentic doctor` shows it.

```yaml
credential_store: auto     # auto (default): the keyring, the file only without one
                           # keyring: never the file; file: the file, skipping the keyring
```

`AGENTIC_CREDENTIAL_STORE` overrides it. Your org can forbid the file with
`credential_file_fallback: false` in its policy; then only the keyring or the
`AGENTIC_<CREDENTIAL>` variables work.

### Jira (`jira:`)

To start a run from an issue (`agentic run feature-delivery --from-jira PROJ-123`) and
have the run report back to it, declare the site. A token is only ever sent to a site
you named:

```yaml
jira:
  url: https://acme.atlassian.net      # Cloud, or a Data Center URL such as https://jira.acme.example/jira
  user: you@acme.example               # Cloud: your account email (with an API token);
                                       # Data Center: leave out for a personal access token
  credential: jira_token               # optional; the default
  api_version: 2                       # optional; 3 for Cloud's v3 (rich-text comments)
  transition: In Progress              # optional; what --from-jira moves the issue to
```

Then run `agentic login --for jira` once. In CI, set `AGENTIC_JIRA_URL`, `AGENTIC_JIRA_USER`
and `AGENTIC_JIRA_TOKEN` instead. The repository's `.sdlc/connectors/jira.json` maps the
issue's fields to the run's request when the defaults do not fit.

The run's pull request is titled `<pipeline>: Jira issue PROJ-123: <summary>`, with the
summary on one line and cut to 80 characters. When Jira is busy (HTTP 429 or 5xx), the
message says how long Jira asked you to wait; when the site cannot be reached, it names
the proxy (`HTTPS_PROXY`, `NO_PROXY`) and CA bundle (`SSL_CERT_FILE`) settings to check.

### Git hosts (`git_hosts:`)

github.com is known. Any other host, such as GitHub Enterprise Server, must be declared
before a pull request is opened there:

```yaml
git_hosts:
  github.acme.com:
    kind: github
    api_url: https://github.acme.com/api/v3   # optional; this is the default
    credential: ghe_token                     # optional; default github_token
```

### Cleaning up finished runs (`gc:`)

`agentic gc` removes finished runs and their trees once they are older than a number of
days. `agentic status` warns when this repository's local data passes a cap. Both
defaults come from the org policy (`limits.yaml` `local:`), else 30 days and 20 GB. Your
own setting wins over the org's:

```yaml
gc:
  older_than_days: 30     # what `agentic gc` removes without --older-than
  disk_cap_gb: 50         # when `agentic status` warns
```

See [CLI operations](runbooks/cli-operations.md) for what gc always keeps.

### The onboarding timer (`~/.agentic/onboarding.json`)

So that your onboarding can be timed without a stopwatch, the CLI keeps the time of your
first try and first success of four commands: `agentic login`, `agentic init`,
`agentic pipelines list` and `agentic run --dry-run`. Times only, no command line, path or
name; the file is never sent anywhere, and it is not touched again once your first dry run
has passed. `agentic onboarding` prints how long it took, `agentic onboarding --reset`
forgets it. Nothing is recorded in CI; set `AGENTIC_ONBOARDING_TIMER=off` to turn it off.

## Org settings: the policy repo's `policy/org.yaml`

### Keeping an environment variable in agent commands (`sandbox.pass_env`)

Agent commands run with a safe environment: variables whose name or value looks secret
are removed. When a command genuinely needs one, for example an authenticated
`HTTPS_PROXY` or `SSH_AUTH_SOCK`, an org admin lists it:

```yaml
sandbox:
  pass_env: [HTTPS_PROXY, SSH_AUTH_SOCK]
```

Names are letters, digits and `_`. The framework's own `AGENTIC_*` variables are never
passed, whatever the list says.

### Resource limits for agent commands (`sandbox.limits`)

What one agent shell command may use: CPU time per process, resident memory of the whole
command, the largest file it may write, and the processes it may run at once. A limit
left out is not applied. Enforced on Linux and macOS; on native Windows best effort, with
no file-size limit (see [CLI operations](runbooks/cli-operations.md)).

```yaml
sandbox:
  limits: { cpu_seconds: 3600, memory_mb: 8192, max_file_mb: 4096, processes: 512 }
```
