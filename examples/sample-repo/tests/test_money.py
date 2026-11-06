from invoices.money import format_cents, total


def test_total_adds_the_lines():
    assert total([1050, 250, 1]) == 1301


def test_format_cents():
    assert format_cents(12345) == "123.45"
    assert format_cents(-5) == "-0.05"
