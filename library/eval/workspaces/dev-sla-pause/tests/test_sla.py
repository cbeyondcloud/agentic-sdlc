from sla import elapsed_minutes


def test_open_ticket_counts_all_time():
    assert elapsed_minutes([(0, "opened")], now=90) == 90


def test_waiting_time_is_not_counted():
    events = [(0, "opened"), (30, "waiting"), (100, "resumed")]
    assert elapsed_minutes(events, now=130) == 60


def test_still_waiting():
    events = [(0, "opened"), (20, "waiting")]
    assert elapsed_minutes(events, now=500) == 20
