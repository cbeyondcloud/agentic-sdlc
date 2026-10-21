from naming import safe_filename


def test_forbidden_characters_are_replaced():
    assert safe_filename("Q1: sales/returns") == "Q1_ sales_returns.csv"


def test_reserved_device_names_are_avoided():
    assert safe_filename("CON") == "_CON.csv"
    assert safe_filename("aux") == "_aux.csv"
    assert safe_filename("com1.backup") == "_com1.backup.csv"


def test_trailing_dots_and_spaces_are_removed():
    assert safe_filename("Draft. ") == "Draft.csv"
