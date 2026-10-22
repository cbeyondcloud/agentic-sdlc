from sizes import human_size


def test_small_and_large():
    assert human_size(512) == "512 B"
    assert human_size(5 * 1024 * 1024) == "5.0 MiB"


def test_unit_boundary_is_1024():
    assert human_size(1023) == "1023 B"
    assert human_size(1024) == "1.0 KiB"
