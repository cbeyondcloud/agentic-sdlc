"""`loadtest.py up | run | status | down` (M3-22). See the README next to `compose.yml`."""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import os
import platform
import secrets
import sys
import tempfile
import time
from pathlib import Path

from agentic_cp.app import utc_now
from agentic_cp.db import engine_for
from agentic_cp.settings import Settings
from cp_loadtest.profile import Timing
from cp_loadtest.run import measure
from cp_loadtest.seed import remove_tree, seed
from cp_loadtest.sim import Options
from cp_loadtest.stack import STATE, External, Stack, target


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    return args.command(args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loadtest.py", description=__doc__)
    sub = parser.add_subparsers(required=True)
    up = sub.add_parser("up", help="build and start the target stack")
    up.add_argument("--replicas", type=int, default=2, help="control-plane replicas (2)")
    up.add_argument("--no-build", action="store_true", help="reuse the built image")
    up.set_defaults(command=_up)
    run = sub.add_parser("run", help="seed fresh tenants and run one load test")
    run.add_argument("--runs", type=int, default=1000, help="concurrent runs (1000)")
    run.add_argument("--duration", type=float, default=600, help="steady seconds (600)")
    run.add_argument("--ramp", type=float, default=120, help="ramp-up seconds (120)")
    run.add_argument("--tenants", type=int, default=2)
    run.add_argument("--teams", type=int, default=10, help="teams per tenant (10)")
    run.add_argument("--step-s", type=float, default=60, help="one step's seconds (60)")
    run.add_argument("--gate-s", type=float, default=30, help="a person's answer (30)")
    run.add_argument("--heartbeat-s", type=float, default=60, help="policy default (60)")
    run.add_argument("--kill-poll-s", type=float, default=10, help="CLI cache TTL (10)")
    run.add_argument("--jitter", type=float, default=0.5)
    run.add_argument("--drain", type=float, default=60, help="seconds to deliver at the end")
    run.add_argument("--connections", type=int, default=200, help="per replica (200)")
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--out", type=Path, default=STATE / "last-run.json")
    run.set_defaults(command=_run)
    status = sub.add_parser("status", help="the stack's containers and replica URLs")
    status.set_defaults(command=_status)
    down = sub.add_parser("down", help="remove the stack's containers and volume")
    down.set_defaults(command=_down)
    return parser


def _up(args) -> int:
    bases = Stack.load(create=True).up(args.replicas, build=not args.no_build)
    print(f"ready: {len(bases)} replica(s): {', '.join(bases)}")
    return 0


def _status(args) -> int:
    stack = Stack.load()
    for name in stack.containers():
        print(name)
    print("replicas:", ", ".join(stack.bases()))
    return 0


def _down(args) -> int:
    Stack.load(for_down=True).down()
    print("removed the agentic-m3-load-test stack")
    return 0


def _run(args) -> int:
    stack = target()
    options = Options(
        runs=args.runs,
        duration_s=args.duration,
        ramp_s=args.ramp,
        timing=Timing(args.step_s, args.gate_s, args.heartbeat_s, args.kill_poll_s, args.jitter),
        seed=args.seed,
        drain_s=args.drain,
    )
    # a run that overruns by 10 minutes prints where every thread is (a hang names itself)
    faulthandler.dump_traceback_later(args.ramp + args.duration + args.drain + 900, exit=False)
    try:
        summary = asyncio.run(_measure(stack, options, args))
    finally:
        faulthandler.cancel_dump_traceback_later()
    summary["machine"] = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "host_cpus": os.cpu_count(),
        **stack.info(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    _print(summary)
    print(f"summary: {args.out}")
    return 0 if all(v["met"] for v in summary["targets"].values()) else 3


async def _measure(stack: Stack | External, options: Options, args) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="cp-loadtest-"))
    settings = Settings.from_env(
        {
            "AGENTIC_CP_DATABASE_URL": stack.dsn("app"),
            "AGENTIC_CP_ALLOW_LOCAL_REPOS": "true",
            "AGENTIC_CP_POLICY_WORKDIR": str(workdir / "cp"),
        }
    )
    owner = engine_for(stack.dsn("owner"), settings.db_schema)
    app = engine_for(stack.dsn("app"), settings.db_schema)
    try:
        started = time.perf_counter()
        population = await seed(
            owner,
            app,
            settings,
            tenants=args.tenants,
            teams=args.teams,
            users=args.runs,
            workdir=workdir,
            clock=utc_now,
            prefix=f"lt{secrets.token_hex(2)}",
        )
        seeded_s = round(time.perf_counter() - started, 1)
        await owner.dispose()
        await app.dispose()
        print(f"seeded {len(population.users)} people in {seeded_s} s; running", file=sys.stderr)
        summary = await measure(
            population,
            stack.bases(),
            options,
            admin_dsn=stack.dsn("admin"),
            engine=stack.engine,
            containers=stack.containers(),
            connections=args.connections,
            metrics_token=stack.secrets.get("LT_METRICS_TOKEN") or None,
        )
        summary["seeding"]["seconds"] = seeded_s
        summary["seeding"]["tenants"] = args.tenants
        summary["seeding"]["teams_per_tenant"] = args.teams
        return summary
    finally:
        await owner.dispose()
        await app.dispose()
        remove_tree(workdir)


def _print(summary: dict) -> None:
    print(
        f"requests {summary['requests']}, errors {summary['errors']} "
        f"({summary['error_rate'] * 100:.3f} %) {summary['errors_by_status']}"
    )
    for name, lag in (
        ("commit", summary["ingest_lag_commit_s"]),
        ("visible", summary["ingest_lag_visible_s"]),
    ):
        print(f"ingest lag {name}: p50 {lag['p50']} s, p95 {lag['p95']} s, p99 {lag['p99']} s")
    for route, row in summary["routes"].items():
        print(
            f"  {route}: n={row['count']} p50 {row['p50_ms']} ms p95 {row['p95_ms']} ms "
            f"p99 {row['p99_ms']} ms {row['statuses']}"
        )
    for name, verdict in summary["targets"].items():
        mark = "met" if verdict["met"] else "MISSED"
        print(f"target {name}: {verdict['value']} (<= {verdict['target']}) {mark}")
    print("counters:", summary["counters"])
    print("database:", summary["database"])
    for name, row in summary["containers"].items():
        print(f"  {name}: {row}")
    print("driver loop lag:", summary["driver_loop_lag"])
