import subprocess
from pathlib import Path

import pytest

GIT = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]

FILES = {
    "pkg/__init__.py": "",
    "pkg/models.py": "class Model:\n    def name(self):\n        return 'model'\n",
    "pkg/service.py": (
        "from pkg.models import Model\n\n\ndef serve():\n    return Model().name()\n"
    ),
    "pkg/api.py": "from pkg.service import serve\n\n\ndef handle():\n    return serve()\n",
    "tests/__init__.py": "",
    "tests/test_models.py": (
        "from pkg.models import Model\n\n\ndef test_name():\n    assert Model().name() == 'model'\n"
    ),
    "tests/test_service.py": (
        "from pkg.service import serve\n\n\ndef test_serve():\n    assert serve() == 'model'\n"
    ),
}


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A small Python package with tests, committed: models <- service <- api."""
    repo = tmp_path / "repo"
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo
