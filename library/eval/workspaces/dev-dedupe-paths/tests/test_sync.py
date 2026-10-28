from sync import dedupe_paths


def test_exact_duplicates_are_dropped():
    assert dedupe_paths(["a.txt", "b.txt", "a.txt"]) == ["a.txt", "b.txt"]


def test_case_sensitive_keeps_both_spellings():
    assert dedupe_paths(["Docs/a.txt", "docs/A.txt"]) == ["Docs/a.txt", "docs/A.txt"]


def test_case_insensitive_keeps_the_first_spelling():
    paths = ["Docs/a.txt", "docs/A.txt", "STRASSE.md", "strasse.md"]
    assert dedupe_paths(paths, case_insensitive=True) == ["Docs/a.txt", "STRASSE.md"]
