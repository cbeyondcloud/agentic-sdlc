from policy import long_enough


def test_ascii_passwords():
    assert long_enough("abcdefgh") and not long_enough("abcdefg")


def test_characters_not_bytes():
    assert not long_enough("éèêëàâ")
