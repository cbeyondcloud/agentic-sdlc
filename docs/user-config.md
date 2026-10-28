# User and org settings people ask about

## Your own settings: `~/.agentic/config.yaml`

`agentic` reads one user config file: `~/.agentic/config.yaml` (`AGENTIC_HOME` instead of
`~/.agentic` when set), on Windows, macOS and Linux alike. Tokens are never written
there. `agentic login --for <name>` keeps them in the OS keyring, and CI passes them as
`AGENTIC_<CREDENTIAL>` environment variables.

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
