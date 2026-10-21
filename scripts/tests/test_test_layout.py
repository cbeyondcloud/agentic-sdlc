"""Test modules need unique file names: pytest's default import mode cannot collect two
`test_x.py` files from different folders (parallel tracks once added two
`test_hardening.py` and the whole suite stopped at collection)."""

import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def duplicate_test_modules(root: Path, testpaths: list[str]) -> dict[str, list[str]]:
    """Test module names used more than once under `testpaths`, skipping folders a
    conftest lists in `collect_ignore` (sample repos and fixtures)."""
    seen: dict[str, list[str]] = defaultdict(list)
    for base in testpaths:
        for path in sorted((root / base).rglob("test_*.py")):
            if _ignored(path, root / base):
                continue
            seen[path.name].append(path.relative_to(root).as_posix())
    return {name: paths for name, paths in seen.items() if len(paths) > 1}


def _ignored(path: Path, base: Path) -> bool:
    for folder in path.relative_to(base).parents:
        conftest = base / folder / "conftest.py"
        if conftest.is_file():
            text = conftest.read_text(encoding="utf-8")
            rest = path.relative_to(base / folder).parts[0]
            if "collect_ignore" in text and f'"{rest}"' in text:
                return True
    return False


def test_two_modules_with_one_name_are_found(tmp_path: Path):
    for folder in ("a", "b"):
        (tmp_path / "tests" / folder).mkdir(parents=True)
        (tmp_path / "tests" / folder / "test_same.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "c").mkdir()
    (tmp_path / "tests" / "c" / "test_other.py").write_text("", encoding="utf-8")
    assert duplicate_test_modules(tmp_path, ["tests"]) == {
        "test_same.py": ["tests/a/test_same.py", "tests/b/test_same.py"]
    }


def test_a_collect_ignored_sample_repo_does_not_count(tmp_path: Path):
    (tmp_path / "tests" / "acc" / "sample" / "tests").mkdir(parents=True)
    (tmp_path / "tests" / "acc" / "conftest.py").write_text(
        'collect_ignore = ["sample"]\n', encoding="utf-8"
    )
    (tmp_path / "tests" / "acc" / "sample" / "tests" / "test_x.py").write_text("", "utf-8")
    (tmp_path / "tests" / "test_x.py").write_text("", encoding="utf-8")
    assert duplicate_test_modules(tmp_path, ["tests"]) == {}


def test_the_repository_has_no_duplicate_test_module_names():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    testpaths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    assert duplicate_test_modules(ROOT, testpaths) == {}
