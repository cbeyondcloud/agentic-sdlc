from retry import retry_delays


def test_doubling():
    assert retry_delays(4) == [1, 2, 4, 8]


def test_delays_are_capped():
    assert retry_delays(10, base=1, max_delay=60)[-3:] == [60, 60, 60]
    assert max(retry_delays(30)) == 60
