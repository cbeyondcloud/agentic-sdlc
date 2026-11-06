# The 15-minute walkthrough

For developers in the pilot training, and anyone trying agentic for the first time. You
sign in, set up the training repository, look at what you can run, do a dry run, then do a
real run whose model answers are recorded, so it spends nothing. The
[developer guide](../guides/developer.md) explains each step in more depth.

**The sample repository** is `invoices`, a tiny Python library (amounts in whole cents)
with a passing test suite. Its source is `examples/sample-repo/` in the agentic
repository, the same folder the acceptance tests run on, so it never drifts from what is
tested. Your platform team publishes it as an internal repository (the
[day-0 runbook](../runbooks/day-0-bootstrap.md#training-environment-m3-23) says how).

## Before you start

Have these ready before the session. They are not part of the 15 minutes.

| What | How |
|---|---|
| The agentic CLI | Steps 1 and 2 of the [developer guide](../guides/developer.md#first-run-about-15-minutes): `agentic --version` works |
| **Windows:** WSL2 with Ubuntu | [Developer guide, "Before you start"](../guides/developer.md#before-you-start). Do everything below in its bash shell |
| Your invite mail | From your team lead; the link works once |
| The training repository's address | From the facilitator, for example `https://git.your-org.internal/training/invoices.git` |

## The walkthrough

Your onboarding time runs from your first `agentic login` to your first successful dry run
(step 5). It is kept on your laptop only; step 5 shows you how to see it.

| Step | Command | Target |
|---|---|---|
| 1 | `agentic login --invite "<link>"` | 2 min |
| 2 | `git clone` the training repository | 1 min |
| 3 | `agentic init` | 5 min |
| 4 | `agentic pipelines list` | 1 min |
| 5 | `agentic run feature-delivery ... --dry-run` | 2 min |
| 6 | a real run on recorded answers | 2 min |
| 7 | `agentic status`, `approve`, `stop` | 2 min |
| | **Total** | **15 min** |

**1. Sign in** with the link from your invite mail. Keep the quotes: the link has a `#`.

```bash
agentic login --invite "https://agentic.your-org.internal/invite#..."
```

It stores your keys in the OS keyring (in WSL2, in `~/.agentic/credentials.json` with a
warning) and writes the control plane and gateway addresses to `~/.agentic/config.yaml`.

**2. Get the training repository.**

```bash
mkdir -p ~/src && cd ~/src
git clone https://git.your-org.internal/training/invoices.git
cd invoices
```

**3. Set it up on your machine.**

```bash
agentic init
```

`init` checks your machine, fetches the org policy and the approved skills, and prints a
readiness report. Fix each item it marks (`agentic init --install-missing` installs the
user-space tools your org allows, after asking), then run it again until it says ready.
Stuck? `agentic doctor` names the fix for each problem.

**4. See what you can run.**

```bash
agentic pipelines list
```

**5. Dry run.** It spends nothing: it shows the steps, the models each tier resolves to,
the budgets, the gates and who must approve each one.

```bash
agentic run feature-delivery --input request="Split an invoice total evenly between several payers" --dry-run
```

You are onboarded. See how long it took, and tell the facilitator the total:

```bash
agentic onboarding
```

**6. A real run, on recorded answers.** Open a second terminal and start the training
gateway. It replays model answers recorded for this walkthrough, on your laptop only: no
model is called and nothing is spent.

```bash
agentic training-gateway
```

It prints a line such as `export AGENTIC_GATEWAY_URL=http://127.0.0.1:53817/c/training/acceptance/1`.
Paste that line into your first terminal, then start the run:

```bash
agentic run feature-delivery --input request="Split an invoice total evenly between several payers"
```

The business analyst writes the requirements, and the run stops at its first gate,
`requirements-approval`, with exit code 4: paused, waiting for a person.

**7. Decide at the gate.**

```bash
agentic status <run>      # where the run is, and its next commands
agentic show <run>        # the requirements the agent wrote
agentic approve <run>     # accept them; the run goes on to the design
```

The run stops again at `design-approval`. In a real run you would read the design and
approve it, and the developer, review and QA agents would build the change and open a pull
request. The recording ends here, so stop the run:

```bash
agentic stop <run> --reason "training walkthrough"
```

Then press Ctrl-C in the second terminal, and remove the gateway setting from the first:

```bash
unset AGENTIC_GATEWAY_URL
```

## What you just saw

- **Gates:** a person decides at each one. `approve`, `reject --feedback "..."` and `edit`
  are the three answers ([developer guide, "Gates"](../guides/developer.md#gates)).
- **No spend in a dry run:** use `--dry-run` whenever you want to see what a run would do.
- **Everything is recorded:** `agentic cost <run>` and `agentic logs <run>` show the run's
  cost and steps. (The recorded answers carry a nominal cost of 0.0125 USD each, billed
  nowhere.)

## If something goes wrong

| What you see | What to do |
|---|---|
| `login` says the invite was already used | Ask your team lead for a new invite |
| `keyring` WARN or FAIL in `agentic doctor` | [Developer guide, "Before you start"](../guides/developer.md#before-you-start) (WSL2 keyring) |
| `init` says the repository is not set up | The training repository was not set up: tell the facilitator |
| `approve` says you may not approve this gate | Tell the facilitator: the training team must let trainees approve their own runs |
| `the run made more calls than recorded`, or `cassette_exhausted` | The training gateway serves one walkthrough: Ctrl-C it, start it again, and start a new run |
| `run` cannot reach `127.0.0.1` with the training gateway running | Your proxy settings send local calls to the proxy: `export NO_PROXY=127.0.0.1,localhost` (add to it if you already have one), then start the run again |
| The gateway prints `N requests differed from the recording (the training pipeline differs)` when stopped | The training team's `feature-delivery` is not the one recorded, so the answers may not fit: tell the facilitator (the [day-0 runbook](../runbooks/day-0-bootstrap.md#training-environment-m3-23) says the training team uses the library pipeline) |
| Anything else | `agentic doctor`, then `agentic support-bundle` for the facilitator; never a screenshot of a token |

Trying it alone, with no org? `agentic init --standalone` sets up a local policy with no
control plane and no sign-in. Follow `templates/standalone/README.md` in the agentic
repository to name your models, then steps 4 to 7 work the same.
