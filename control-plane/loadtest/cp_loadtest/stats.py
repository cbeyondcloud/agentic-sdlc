"""What the load test measured, and whether it met the targets (M3-22).

A request is an error when it did not get the answer it asked for: a transport error or
timeout, any 5xx, a 429, and any other 4xx. The kill poll's 304 is an answer ("nothing
changed"). Ingest lag has two clocks: `commit_lag` ends when the ingest response that
carried the event returned 2xx (client queueing, 429 waits and gap catch-ups count), and
`visible_lag` when a separate database reader first saw the run's head at or past it."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

LAG_ROUND = 3


def percentile(values: list[float], q: float) -> float | None:
    """The nearest-rank `q`th percentile, or None without values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def is_error(status: int | str) -> bool:
    return not isinstance(status, int) or (status >= 400)


class Recorder:
    """Every request's route, status and seconds; the lag samples; run counters."""

    def __init__(self) -> None:
        self.latency: dict[str, list[float]] = defaultdict(list)
        self.statuses: dict[str, Counter] = defaultdict(Counter)
        self.commit_lag: list[float] = []
        self.visible_lag: list[float] = []
        self.counters: Counter = Counter()

    def request(self, route: str, status: int | str, seconds: float) -> None:
        self.latency[route].append(seconds)
        self.statuses[route][status] += 1

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def summary(self) -> dict:
        routes, errors, total = {}, Counter(), 0
        for route in sorted(self.latency):
            seconds = self.latency[route]
            statuses = self.statuses[route]
            total += len(seconds)
            errors.update({str(s): n for s, n in statuses.items() if is_error(s)})
            routes[route] = {
                "count": len(seconds),
                "p50_ms": _ms(percentile(seconds, 50)),
                "p95_ms": _ms(percentile(seconds, 95)),
                "p99_ms": _ms(percentile(seconds, 99)),
                "max_ms": _ms(max(seconds)),
                "statuses": {str(s): n for s, n in sorted(statuses.items(), key=str)},
            }
        failed = sum(errors.values())
        return {
            "requests": total,
            "errors": failed,
            "error_rate": round(failed / total, 6) if total else 0.0,
            "errors_by_status": dict(sorted(errors.items())),
            "routes": routes,
            "ingest_lag_commit_s": _lag(self.commit_lag),
            "ingest_lag_visible_s": _lag(self.visible_lag),
            "counters": dict(sorted(self.counters.items())),
        }


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 1)


def _lag(values: list[float]) -> dict:
    def r(value: float | None) -> float | None:
        return None if value is None else round(value, LAG_ROUND)

    return {
        "count": len(values),
        "p50": r(percentile(values, 50)),
        "p95": r(percentile(values, 95)),
        "p99": r(percentile(values, 99)),
        "max": r(max(values) if values else None),
    }


@dataclass(frozen=True)
class Targets:
    """The proposed M3-22 targets (owner decision pending): see the plan and the report."""

    ingest_lag_p95_s: float = 5.0
    ingest_lag_p99_s: float = 15.0
    error_rate: float = 0.001
    kill_poll_p99_s: float = 2.0
    heartbeat_p99_s: float = 2.0
    register_p95_s: float = 2.0

    def verdicts(self, summary: dict) -> dict[str, dict]:
        lag = summary["ingest_lag_visible_s"]
        routes = summary["routes"]

        def route(name: str, field: str) -> float | None:
            found = routes.get(name, {}).get(field)
            return None if found is None else found / 1000

        values = {
            "ingest_lag_p95_s": lag["p95"],
            "ingest_lag_p99_s": lag["p99"],
            "error_rate": summary["error_rate"] if summary["requests"] else None,
            "kill_poll_p99_s": route("GET /v1/killswitch/state", "p99_ms"),
            "heartbeat_p99_s": route("POST /v1/runs/{run_id}/heartbeat", "p99_ms"),
            "register_p95_s": route("POST /v1/runs", "p95_ms"),
        }
        limits = asdict(self)
        return {
            name: {
                "target": limits[name],
                "value": value,
                "met": value is not None and value <= limits[name],
            }
            for name, value in values.items()
        }
