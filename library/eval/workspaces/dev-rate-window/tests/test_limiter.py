from limiter import Limiter


def test_limit_applies_inside_the_window():
    limiter = Limiter(limit=2, window=60)
    assert limiter.allow("u", 0) and limiter.allow("u", 10)
    assert not limiter.allow("u", 20)


def test_an_attempt_exactly_a_window_old_has_expired():
    limiter = Limiter(limit=1, window=60)
    assert limiter.allow("u", 0)
    assert limiter.allow("u", 60)


def test_old_attempts_are_dropped():
    limiter = Limiter(limit=5, window=60)
    for second in range(0, 6000, 30):
        limiter.allow("u", second)
    assert len(limiter.attempts["u"]) <= 5
