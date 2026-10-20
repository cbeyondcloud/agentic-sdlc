"""S10 throwaway: an A/B of a "skill" on pydantic-evals 2.54 with no network, no real model.

uv run --no-project --python 3.12 --with pydantic-evals==2.54.0 python spikes/s10_ab_eval/toy_ab.py

What it proves about the proposed harness shape:
  1. pydantic-evals runs an arbitrary async task per case with `repeat=k` and groups the
     repeats per case (`report.case_groups()`), so the harness need not re-implement replay.
  2. A custom pointwise rubric Evaluator can call a judge `Agent` on ANY pydantic-ai Model
     (here FunctionModel; in production the TokenLean-backed model from agentic.transport).
  3. Blinding: the judge only ever sees scrubbed text; a leak check proves no variant label,
     skill name or skill "tell" reached it.
  4. Pairwise judging with position swap removes a judge's first-position bias.
  5. Paired stats on per-case means (sign-flip decision + bootstrap CI) from abstats.
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

sys.path.insert(0, str(Path(__file__).parent))
import abstats  # noqa: E402

# ---------------------------------------------------------------- the candidate "agent"
REQUESTS = [
    "Add CSV export to the orders page",
    "Rate-limit the public search endpoint",
    "Let admins deactivate a user account",
    "Send a weekly digest email of open tickets",
    "Support refunds on partially shipped orders",
    "Show the last login time on the profile page",
    "Retry failed webhook deliveries",
    "Allow bulk tagging of products",
]
RNG = random.Random(42)  # stands in for model nondeterminism at production temperature


def candidate(with_skill: bool):
    """A fake BA agent. The 'skill' makes acceptance criteria more likely, and like many
    real skills it announces itself in the output (a tell the scrubber must remove)."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1].parts[-1].content  # type: ignore[union-attr]
        lines = [f"## Requirements: {request}", "Users need this because ..."]
        p_ac = 0.75 if with_skill else 0.45
        n_ac = sum(RNG.random() < p_ac for _ in range(4))
        lines += [f"- AC{i + 1}: Given ... when ... then ..." for i in range(n_ac)]
        if RNG.random() < (0.6 if with_skill else 0.3):
            lines.append("Out of scope: ...")
        if with_skill:
            lines.insert(0, "Using skill: acceptance-criteria-writer")
        return ModelResponse(parts=[TextPart("\n".join(lines))])

    return Agent(FunctionModel(fn), name="ba")


# ---------------------------------------------------------------- blinding
TELLS = [re.compile(r"^Using skill:.*$", re.M), re.compile(r"acceptance-criteria-writer")]
FORBIDDEN = ["skill", "variant", "baseline", "candidate"]
SEEN_BY_JUDGE: list[str] = []


def scrub(text: str) -> str:
    for pat in TELLS:
        text = pat.sub("", text)
    return text.strip()


# ---------------------------------------------------------------- pointwise rubric judge
class Criterion(BaseModel):
    score: int = Field(ge=0, le=2)
    evidence: str  # a quote from the output: forces grounded scores


class RubricScore(BaseModel):
    problem_statement: Criterion
    acceptance_criteria: Criterion
    testability: Criterion
    scope_boundaries: Criterion
    consistency: Criterion

    @property
    def total(self) -> int:
        return sum(c.score for c in self.__dict__.values())


def judge_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A deterministic fake judge: counts features; in production this is a different
    model family from the candidate, at temperature 0, pinned by model id."""
    text = messages[-1].parts[-1].content  # type: ignore[union-attr]
    SEEN_BY_JUDGE.append(text)
    n_ac = text.count("AC")
    c = {
        "problem_statement": 2 if "because" in text else 1,
        "acceptance_criteria": min(2, n_ac // 2),
        "testability": 2 if n_ac >= 3 else (1 if n_ac else 0),
        "scope_boundaries": 2 if "Out of scope" in text else 0,
        "consistency": 2,
    }
    args = {k: {"score": v, "evidence": "..."} for k, v in c.items()}
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])


judge_agent = Agent(FunctionModel(judge_fn), output_type=RubricScore, name="judge")


@dataclass
class BlindRubric(Evaluator):
    rubric_id: str = "ba-requirements@1"

    async def evaluate(self, ctx: EvaluatorContext) -> dict[str, float]:
        res = await judge_agent.run(f"<output>\n{scrub(ctx.output)}\n</output>")
        return {"rubric": float(res.output.total)}


# ---------------------------------------------------------------- pairwise judge
class Preference(BaseModel):
    winner: int = Field(ge=0, le=2)  # 1, 2, or 0 = tie
    reason: str


def pair_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    text = messages[-1].parts[-1].content  # type: ignore[union-attr]
    SEEN_BY_JUDGE.append(text)
    r1, r2 = text.split("<response_2>")
    a, b = r1.count("AC") + 2 * ("Out of scope" in r1), r2.count("AC") + 2 * ("Out of scope" in r2)
    # deliberate first-position bias: a near tie goes to Response 1
    winner = 1 if a >= b - 1 else 2
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"winner": winner, "reason": "..."})]
    )


pair_agent = Agent(FunctionModel(pair_fn), output_type=Preference, name="pair-judge")


async def pairwise(out_a: str, out_b: str) -> tuple[float, bool]:
    """Judge both orders. Returns (score for B in [0, 1], consistent?). A win counts only
    if it survives the swap; an order-dependent verdict is a tie (0.5)."""
    a, b = scrub(out_a), scrub(out_b)
    p1 = (await pair_agent.run(f"<response_1>{a}<response_2>{b}")).output.winner
    p2 = (await pair_agent.run(f"<response_1>{b}<response_2>{a}")).output.winner
    b_first = {1: 0.0, 2: 1.0, 0: 0.5}[p1]  # B shown second in order 1
    b_second = {1: 1.0, 2: 0.0, 0: 0.5}[p2]  # B shown first in order 2
    if b_first == b_second:
        return b_first, True
    return 0.5, False


# ---------------------------------------------------------------- run
async def main() -> None:
    k = 3
    ds = Dataset[str, str, dict](
        name="ba-scoping-toy",
        cases=[Case(name=f"case-{i:02d}", inputs=r) for i, r in enumerate(REQUESTS)],
        evaluators=[BlindRubric()],
    )

    def task(agent):
        async def run(inputs: str) -> str:
            return (await agent.run(inputs)).output

        return run

    rep_a = await ds.evaluate(task(candidate(False)), name="A", repeat=k, progress=False)
    rep_b = await ds.evaluate(task(candidate(True)), name="B", repeat=k, progress=False)

    def by_case(rep) -> tuple[dict[str, list[float]], dict[str, list[str]]]:
        scores, outs = {}, {}
        for g in rep.case_groups() or []:
            scores[g.name] = [r.scores["rubric"].value for r in g.runs]
            outs[g.name] = [r.output for r in g.runs]
        return scores, outs

    sa, oa = by_case(rep_a)
    sb, ob = by_case(rep_b)
    print(f"cases={len(sa)} repeats={k}: groups per case ok =",
          all(len(v) == k for v in (*sa.values(), *sb.values())))
    d = abstats.paired_diffs(sa, sb)
    print("per-case rubric diffs (B-A):", [round(x, 2) for x in d])
    print("sign-flip superiority:", {k_: round(v, 4) if isinstance(v, float) else v
                                     for k_, v in abstats.signflip_test(d).items()})
    print("bootstrap:", {k_: round(v, 3) if isinstance(v, float) else v
                         for k_, v in abstats.paired_bootstrap(d).items()})

    # pairwise, run r of A vs run r of B
    wins, inconsistent, one_order_only = [], 0, []
    for cid in sorted(oa):
        for r in range(k):
            w, ok = await pairwise(oa[cid][r], ob[cid][r])
            wins.append(w)
            inconsistent += not ok
            p1 = (await pair_agent.run(
                f"<response_1>{scrub(oa[cid][r])}<response_2>{scrub(ob[cid][r])}")).output.winner
            one_order_only.append({1: 0.0, 2: 1.0, 0: 0.5}[p1])
    print(f"pairwise B win-rate with swap = {sum(wins) / len(wins):.2f} "
          f"({inconsistent}/{len(wins)} order-dependent -> tie); "
          f"A-always-first without swap = {sum(one_order_only) / len(one_order_only):.2f}")

    leaks = [w for t in SEEN_BY_JUDGE for w in FORBIDDEN if w in t.lower()]
    print(f"judge calls={len(SEEN_BY_JUDGE)} blinding leaks={leaks or 'none'}")


if __name__ == "__main__":
    asyncio.run(main())
