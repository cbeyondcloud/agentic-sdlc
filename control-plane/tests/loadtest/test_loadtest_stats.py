"""The load harness's numbers: percentiles, error rate and the target verdicts (M3-22)."""

from cp_loadtest.stats import Recorder, Targets, is_error, percentile


def test_percentile_is_nearest_rank():
    values = [float(n) for n in range(1, 101)]
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 99) == 99
    assert percentile([3.0], 99) == 3.0
    assert percentile([], 95) is None


def test_errors_are_every_failure_but_the_kill_polls_304():
    assert not is_error(200) and not is_error(201) and not is_error(304)
    for status in (409, 422, 429, 500, 503, "timeout", "connect"):
        assert is_error(status)


def test_summary_percentiles_and_error_rate():
    recorder = Recorder()
    for n in range(1, 101):
        recorder.request("GET /v1/killswitch/state", 200 if n % 2 else 304, n / 1000)
    recorder.request("POST /v1/runs/{run_id}/events", 500, 0.2)
    recorder.request("POST /v1/runs/{run_id}/events", "timeout", 15.0)
    recorder.visible_lag += [0.1] * 95 + [4.0] * 4 + [20.0]
    recorder.commit_lag += [0.05] * 100
    recorder.count("runs_registered", 3)
    summary = recorder.summary()
    kill = summary["routes"]["GET /v1/killswitch/state"]
    assert kill["count"] == 100 and kill["p50_ms"] == 50 and kill["p99_ms"] == 99
    assert kill["statuses"] == {"200": 50, "304": 50}
    assert summary["requests"] == 102 and summary["errors"] == 2
    assert summary["error_rate"] == round(2 / 102, 6)
    assert summary["errors_by_status"] == {"500": 1, "timeout": 1}
    assert summary["ingest_lag_visible_s"] == {
        "count": 100,
        "p50": 0.1,
        "p95": 0.1,
        "p99": 4.0,
        "max": 20.0,
    }
    assert summary["counters"]["runs_registered"] == 3


def test_targets_verdicts():
    recorder = Recorder()
    recorder.visible_lag += [1.0] * 100
    for _ in range(1000):
        recorder.request("GET /v1/killswitch/state", 304, 0.01)
        recorder.request("POST /v1/runs/{run_id}/heartbeat", 200, 0.01)
        recorder.request("POST /v1/runs", 200, 0.1)
    recorder.request("POST /v1/runs", 503, 0.1)
    verdicts = Targets().verdicts(recorder.summary())
    assert all(v["met"] for name, v in verdicts.items() if name != "error_rate"), verdicts
    assert verdicts["error_rate"]["met"]  # 1 in 3001 < 0.1 %
    recorder.visible_lag += [30.0] * 10
    assert not Targets().verdicts(recorder.summary())["ingest_lag_p95_s"]["met"]
    assert not Targets(error_rate=0.0001).verdicts(recorder.summary())["error_rate"]["met"]


def test_targets_without_samples_are_not_met():
    verdicts = Targets().verdicts(Recorder().summary())
    assert not verdicts["ingest_lag_p95_s"]["met"] and verdicts["ingest_lag_p95_s"]["value"] is None
