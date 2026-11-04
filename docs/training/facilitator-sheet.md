# Facilitator sheet: timing onboarding (M3-23)

**The measure.** M3-23 is done when **5 pilot developers onboard unaided in 15 minutes or
less, median**. Onboarding is timed from the developer's first `agentic login` to their
first successful `agentic run --dry-run` (steps 1 to 5 of the
[walkthrough](walkthrough.md)). The CLI records it on the developer's own laptop
(`~/.agentic/onboarding.json`, times only) and sends it nowhere; the developer reads it
out with `agentic onboarding`.

## Before the session

- [ ] Each developer has the CLI installed (`agentic --version`) and an unused invite.
      Installing is not part of the 15 minutes; note anyone who could not install.
- [ ] The training repository and team are set up
      ([day-0 runbook](../runbooks/day-0-bootstrap.md#training-environment-m3-23)).
- [ ] Anyone who rehearsed on their laptop ran `agentic onboarding --reset`.
- [ ] At least 5 developers, a mix of Windows (WSL2) and macOS laptops.

## During the session

- **Unaided** means the developer follows the walkthrough and the error messages alone.
  Do not touch their keyboard or tell them a command. If they ask, say "what does the
  message suggest?" once; if they are still stuck after 5 minutes, help, and mark the row
  **aided** (it does not count towards the 5).
- Note every place someone hesitated or asked: each is an office-hours item
  ([intake](office-hours.md#from-an-issue-to-the-tracker)).

## After step 5

Each developer runs:

```bash
agentic onboarding
```

and reads out the total and the step times (or `agentic --json onboarding` into the
tracking issue). Write them down:

| # | Developer (initials) | OS | Unaided? | login | init | pipelines list | dry run | **Total** | Where they got stuck |
|---|---|---|---|---|---|---|---|---|---|
| 1 | | | | | | | | | |
| 2 | | | | | | | | | |
| 3 | | | | | | | | | |
| 4 | | | | | | | | | |
| 5 | | | | | | | | | |
| 6 | | | | | | | | | |
| 7 | | | | | | | | | |

## The median

Sort the **unaided** totals from shortest to longest; the median is the middle one (with
an even count, the mean of the two middle ones). With 5 times, it is the 3rd.

| | |
|---|---|
| Unaided developers timed | |
| Median total | |
| Longest total, and why | |
| Windows median / macOS median | |
| **Met (median 15 min or less, at least 5 unaided)?** | yes / no |

Record the sheet in the pilot's tracking issue, and the result on the M3-23 row of the
tracker: the date, the count, the median and the link. If the median is over 15 minutes,
the slowest step's causes become office-hours items, and the measurement is repeated with
the next group.
