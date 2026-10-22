"""`agentic eval run` and `agentic eval calibrate export|score` (M1-63, spike S10).

    agentic eval run --agent <name> [--corpus DIR] [--split holdout|dev|all] [--repeat N]
                     [--mode live|record|replay] [--budget-usd N] [--out DIR]
                     [--policy DIR] [--agents DIR] [--cassettes DIR] [--gateway URL]
                     [--classification CLASS]
    agentic eval calibrate export --report DIR [--out FILE]
    agentic eval calibrate score  --report DIR --scores FILE --scores FILE

Exit codes: 0 a report was written and measured; 1 the replay failed, a blinding leak
voided the run, or nothing could be scored; 2 refused (no judge tier, a class whose
tiers exclude the agent's or the judge's, no key, a judge that fails calibration); 3 an
invalid corpus, policy or agent, or cases of mixed classes; 5 the budget cap stopped
the run. The report is written (and shown) even when the exit code is not 0.
"""

from pathlib import Path
from typing import Annotated

import typer

from agentic_cli import evaluation
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output


def register(app: typer.Typer) -> None:
    group = typer.Typer(
        help="Evaluate agents: run them over an eval corpus, calibrate the judge.",
        no_args_is_help=True,
    )
    calibrate = typer.Typer(
        help="Check the rubric judge against people (blinded).", no_args_is_help=True
    )
    group.add_typer(calibrate, name="calibrate")
    app.add_typer(group, name="eval")

    @group.command("run")
    def run(
        ctx: typer.Context,
        agent: Annotated[str, typer.Option("--agent", help="The agent definition's name.")],
        corpus: Annotated[
            Path | None, typer.Option(help="The eval corpus folder (default: .agentic/eval).")
        ] = None,
        split: Annotated[str, typer.Option(help="holdout, dev or all.")] = "holdout",
        repeat: Annotated[
            int | None, typer.Option(min=1, help="Runs per case (default: policy eval.repeats).")
        ] = None,
        mode: Annotated[
            str, typer.Option(help="live (TokenLean), record (TokenLean, saved) or replay.")
        ] = "live",
        budget_usd: Annotated[
            float | None,
            typer.Option("--budget-usd", min=0.01, help="Spend cap (default: policy eval budget)."),
        ] = None,
        out: Annotated[Path | None, typer.Option(help="Where to write the report.")] = None,
        policy: Annotated[Path | None, typer.Option(help="The org policy folder.")] = None,
        agents: Annotated[Path | None, typer.Option(help="Agent definitions folder.")] = None,
        cassettes: Annotated[
            Path | None, typer.Option(help="Recorded responses (record/replay).")
        ] = None,
        gateway: Annotated[str | None, typer.Option(help="TokenLean's URL.")] = None,
        seed: Annotated[int, typer.Option(help="Seed for run order and statistics.")] = 0,
        concurrency: Annotated[int, typer.Option(min=1, help="Runs at once.")] = 1,
        classification: Annotated[
            str | None,
            typer.Option(
                help="Data class of cases that name none (default: the org's default class)."
            ),
        ] = None,
    ) -> None:
        """Run an agent over an eval corpus and write a report."""
        output: Output = ctx.obj
        result = evaluation.execute_run(
            evaluation.RunOptions(
                agent=agent,
                corpus=corpus,
                split=split,
                repeat=repeat,
                mode=mode,
                budget_usd=budget_usd,
                out=out,
                policy=policy,
                agents=agents,
                cassettes=cassettes,
                gateway=gateway,
                seed=seed,
                max_concurrency=concurrency,
                classification=classification,
            ),
            warn=output.warn,  # before anything is spent (a judge family it cannot tell)
        )
        data = evaluation.summary(result)
        output.result(data, lambda console: _show_run(console, data))
        if (error := evaluation.failure(result)) is not None:
            raise error

    @calibrate.command("export")
    def export(
        ctx: typer.Context,
        report: Annotated[Path, typer.Option(help="The folder `eval run` wrote.")],
        out: Annotated[Path | None, typer.Option(help="Where to write the pack.")] = None,
        seed: Annotated[int, typer.Option(help="Seed for the sample.")] = 0,
    ) -> None:
        """Write a blinded sample of judged outputs for two people to score."""
        output: Output = ctx.obj
        data = evaluation.export_calibration(report, out, seed=seed)
        output.result(
            data,
            lambda console: console.print(
                f"{data['items']} items in {data['pack']}; each rater fills a copy of "
                f"{data['template']}",
                markup=False,
            ),
        )

    @calibrate.command("score")
    def score(
        ctx: typer.Context,
        report: Annotated[Path, typer.Option(help="The folder `eval run` wrote.")],
        scores: Annotated[list[Path], typer.Option(help="A rater's filled scores (twice).")],
    ) -> None:
        """Score the judge against the raters (weighted kappa)."""
        output: Output = ctx.obj
        data = evaluation.score_calibration(report, scores)
        output.result(data, lambda console: console.print(_calibration_line(data), markup=False))
        if data["verdict"] != "ACCEPTED":
            raise CliError(
                f"the judge is not accepted for this rubric: {data['verdict']}",
                ExitCode.REFUSED,
                hint=(
                    "fix the rubric's anchors, then calibrate again"
                    if data["verdict"] == "RUBRIC_AMBIGUOUS"
                    else "ask an admin for another eval-judge model, or refine the rubric"
                ),
            )


def _show_run(console, data: dict) -> None:
    console.print(
        f"{data['agent']}: {data['verdict']} - {data['cases']} cases x {data['repeats']} repeats",
        markup=False,
    )
    for label, metrics in data["aggregates"].items():
        for name, value in metrics.items():
            shown = "-" if value is None else f"{value:.3f}"
            console.print(f"  {label} {name}: {shown}", markup=False)
    if data["stale_cassettes"]:
        console.print(
            f"  {data['stale_cassettes']} stale cassettes: prompts changed; re-record soon",
            markup=False,
        )
    console.print(f"report: {data['report']}", markup=False)


def _calibration_line(data: dict) -> str:
    return (
        f"{data['verdict']}: kappa(judge, raters) {data['kappa_judge']}, within 1 point "
        f"{data['within_1']:.0%}, kappa(raters) {data['kappa_raters']} over {data['items']} items"
    )
