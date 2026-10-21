from manifest import to_posix


def test_posix_paths_are_kept():
    assert to_posix("src//app/main.py") == "src/app/main.py"


def test_windows_separators_become_slashes():
    assert to_posix("src\\app\\main.py") == "src/app/main.py"
    assert to_posix("C:\\work\\repo\\a.txt") == "C:/work/repo/a.txt"
