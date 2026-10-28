from ignore import is_ignored


def test_posix_paths_match():
    assert is_ignored("build/out/app.js", ["build/*"])
    assert not is_ignored("src/app.js", ["build/*"])


def test_windows_paths_match():
    assert is_ignored("build\\out\\app.js", ["build/*"])
    assert is_ignored("node_modules\\x\\index.js", ["node_modules/*"])
