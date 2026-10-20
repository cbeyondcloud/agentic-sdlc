from money import split_evenly


def test_even_split():
    assert split_evenly(900, 3) == [300, 300, 300]


def test_remainder_is_not_lost():
    shares = split_evenly(1000, 3)
    assert sum(shares) == 1000
    assert max(shares) - min(shares) <= 1
