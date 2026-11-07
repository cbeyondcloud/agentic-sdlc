"""The spend caps of the first live runs (DF-108, owner decision 2026-11-07).

    uv run python scripts/live_caps.py check
    uv run python scripts/live_caps.py remaining <campaign> [--agent NAME] --spent-in DIR...

`check` loads docs/runbooks/live-spend-caps.yaml and prints the caps. `remaining` prints
what a campaign may still spend, in USD with cents (rounded down), counting every earlier
attempt found under the `--spent-in` folders - reruns included: each eval `report.json`
(its runs' cost plus its judge's, live and record mode only; a replay bills nothing) and
each run's `run.json` (its budget ledger, open steps included). With `--agent` it is the
tighter of that agent's share and the campaign's total. At the cap it prints nothing on
stdout, says so on stderr and exits 1, so a runbook loop stops there. Exit 2: a bad caps
file, campaign or agent.

The amount is what `agentic run --budget` or `agentic eval run --budget-usd` takes: the run
then stops and reports when the cap is hit. The provider key's own credit limit is the
outer backstop (`provider_key_credit_limit_usd`).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

from agentic.transport.budget import BudgetLedger
from agentic.yamlio import YamlFileError, load_yaml, read_text

REPO = Path(__file__).resolve().parent.parent
CAPS = REPO / "docs" / "runbooks" / "live-spend-caps.yaml"
PAID_MODES = ("live", "record")
CENT = Decimal("0.01")


class CapsError(Exception):
    """The caps file, a campaign or an agent is not usable."""


@dataclass(frozen=True)
class Caps:
    credit_limit: Decimal
    campaigns: dict[str, dict]

    def _campaign(self, name: str) -> dict:
        if name not in self.campaigns:
            raise CapsError(f"no campaign {name!r} (known: {', '.join(sorted(self.campaigns))})")
        return self.campaigns[name]

    def total(self, campaign: str) -> Decimal:
        return self._campaign(campaign)["total"]

    def by_agent(self, campaign: str) -> dict[str, Decimal]:
        return dict(self._campaign(campaign)["by_agent"])


def _usd(value: object, where: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise CapsError(f"{where} is not an amount in USD: {value!r}") from None
    if not amount.is_finite() or amount <= 0:
        raise CapsError(f"{where} must be above zero, not {value!r}")
    return amount


def load_caps(path: Path = CAPS) -> Caps:
    try:
        data, _ = load_yaml(read_text(path))
    except (OSError, YamlFileError) as error:
        raise CapsError(f"{path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("campaigns"), dict):
        raise CapsError(f"{path}: expected `campaigns:` with one entry per campaign")
    credit = _usd(data.get("provider_key_credit_limit_usd"), "provider_key_credit_limit_usd")
    campaigns: dict[str, dict] = {}
    for name, entry in data["campaigns"].items():
        entry = entry if isinstance(entry, dict) else {}
        total = _usd(entry.get("total_usd"), f"{name}.total_usd")
        split = {
            agent: _usd(usd, f"{name}.by_agent.{agent}")
            for agent, usd in (entry.get("by_agent") or {}).items()
        }
        if sum(split.values(), Decimal(0)) > total:
            raise CapsError(f"{name}: by_agent adds up to more than total_usd ({total})")
        campaigns[name] = {"total": total, "by_agent": split}
    together = sum((c["total"] for c in campaigns.values()), Decimal(0))
    if together > credit:
        raise CapsError(
            f"the campaigns add up to {together} USD, more than the provider key's credit "
            f"limit ({credit}): the backstop must cover them"
        )
    return Caps(credit, campaigns)


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _report_spend(data: dict, agent: str | None) -> Decimal:
    if data.get("mode") not in PAID_MODES or (agent is not None and data.get("agent") != agent):
        return Decimal(0)
    costs = (data.get("cost_total_usd"), data.get("judge_cost_usd"))
    return sum((Decimal(str(c)) for c in costs if c is not None), Decimal(0))


def _run_spend(data: dict) -> Decimal:
    budget = data.get("budget")
    if not isinstance(budget, dict) or not budget:
        return Decimal(0)
    return BudgetLedger.restore(budget).run.total_spent()


def spent(folders: list[Path], agent: str | None = None) -> Decimal:
    """What every attempt under `folders` recorded; eval reports only for `agent`."""
    total = Decimal(0)
    for folder in folders:
        for path in sorted(Path(folder).rglob("report.json")):
            total += _report_spend(_json(path), agent)
        if agent is None:  # a run's spend has no single agent: it counts for the campaign
            for path in sorted(Path(folder).rglob("run.json")):
                total += _run_spend(_json(path))
    return total


def remaining(caps: Caps, campaign: str, folders: list[Path], agent: str | None = None) -> Decimal:
    left = caps.total(campaign) - spent(folders)
    if agent is not None:
        split = caps.by_agent(campaign)
        if agent not in split:
            raise CapsError(f"{campaign} has no cap for agent {agent!r} ({', '.join(split)})")
        left = min(left, split[agent] - spent(folders, agent))
    return left


def eval_budgets(caps: Caps, campaign: str) -> dict:
    """The campaign's per-agent caps as the eval policy's budgets (`eval:` in policy): an
    agent the campaign does not name gets the smallest share, never more."""
    split = caps.by_agent(campaign)
    if not split:
        raise CapsError(f"{campaign} has no per-agent caps (by_agent)")
    return {
        "budget_usd": float(min(split.values())),
        "budget_usd_by_agent": {agent: float(usd) for agent, usd in split.items()},
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="live_caps.py", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Load the caps file and print it.")
    left = commands.add_parser("remaining", help="What a campaign may still spend, USD.")
    left.add_argument("campaign")
    left.add_argument("--agent")
    left.add_argument("--spent-in", action="append", type=Path, default=[], dest="spent_in")
    args = parser.parse_args(argv)
    try:
        caps = load_caps()
        if args.command == "check":
            print(f"provider key credit limit: {caps.credit_limit} USD")
            for name in caps.campaigns:
                shares = ", ".join(f"{a} {u}" for a, u in caps.by_agent(name).items())
                print(f"{name}: {caps.total(name)} USD" + (f" ({shares})" if shares else ""))
            return 0
        amount = remaining(caps, args.campaign, args.spent_in, args.agent)
    except CapsError as error:
        print(f"live_caps: {error}", file=sys.stderr)
        return 2
    if amount < CENT:
        cap = caps.total(args.campaign)
        used = spent(args.spent_in)
        who = f" (agent {args.agent})" if args.agent else ""
        print(
            f"cap reached: {args.campaign}{who} has spent {used.quantize(CENT)} of {cap} USD "
            "- stop here and report",
            file=sys.stderr,
        )
        return 1
    print(amount.quantize(CENT, rounding=ROUND_DOWN))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
