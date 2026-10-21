from versions import is_newer


def test_simple_ordering():
    assert is_newer("1.2.0", "1.1.9")
    assert not is_newer("1.1.0", "1.1.0")


def test_numeric_components():
    assert is_newer("1.10.0", "1.9.0")
    assert not is_newer("1.9.0", "1.10.0")


def test_pre_releases_are_older():
    assert is_newer("2.0.0", "2.0.0-rc1")
    assert not is_newer("2.0.0-rc1", "2.0.0")
