"""Runner evidence tiers over Zyroflo's provider smoke (M2-41; D34: reuse, never rewrite).

Zyroflo's `RUNNER_CONTRACT.md` and `zyroflo.provider_smoke` record, with every conformance
verdict, what kind of runner produced it - because a green from a fixed command sequence
and a green from a live agent are different claims:

  * `null`     - no runner at all: the harness's own fixed sequence;
  * `contract` - a runner that honours the contract but has no model in it: for the
                 framework's backends, their scripted stand-ins (a scripted model, a fake
                 agent CLI, a gateway stub in front of the pinned binary);
  * `agent`    - a live agent with a real model behind it.

A stronger tier satisfies a weaker requirement. The framework's backend conformance suite
runs at `contract`; `agent` evidence needs live models, which wait until the framework is
complete (owner decision 73). The tier vocabulary and its order are Zyroflo's; this
module only puts them behind the framework's names.
"""

from zyroflo import provider_smoke

#: Zyroflo's runner tiers, weakest first.
RUNNER_TIERS: tuple[str, ...] = tuple(provider_smoke.RUNNER_TIERS)
SCRIPTED = "contract"  # what the backend conformance suite's stand-ins prove
LIVE = "agent"
MEANING = {
    "null": "no runner: a fixed command sequence",
    "contract": "a scripted stand-in honours the contract; no model was asked",
    "agent": "a live agent with a real model behind it",
}


def rank(tier: str) -> int:
    """The tier's place, weakest first. ValueError for anything else."""
    if tier not in RUNNER_TIERS:
        raise ValueError(f"{tier!r} is not a runner tier: one of {', '.join(RUNNER_TIERS)}")
    return RUNNER_TIERS.index(tier)


def satisfies(have: str, need: str) -> bool:
    """Whether evidence at tier `have` answers a requirement for tier `need`."""
    return rank(have) >= rank(need)
