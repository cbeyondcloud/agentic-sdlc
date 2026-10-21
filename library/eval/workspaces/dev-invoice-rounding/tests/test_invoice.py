from invoice import line_total


def test_plain_totals():
    assert line_total("19.99", 3) == "59.97"


def test_half_cents_round_up():
    assert line_total("2.675", 1) == "2.68"
    assert line_total("0.125", 1) == "0.13"
    assert line_total("1.005", 3) == "3.02"
