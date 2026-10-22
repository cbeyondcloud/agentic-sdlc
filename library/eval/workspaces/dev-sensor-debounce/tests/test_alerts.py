from alerts import should_alert


def test_sustained_breach_alerts():
    assert should_alert([3, 9, 9, 9], threshold=5, needed=3)


def test_interrupted_breaches_do_not_alert():
    assert not should_alert([9, 9, 3, 9, 9, 3], threshold=5, needed=3)
