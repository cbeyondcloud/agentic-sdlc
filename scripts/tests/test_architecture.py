"""The architecture rules (P0-03): import-linter contracts in pyproject.toml hold for the
repo, and a deliberate violation fails."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINT = shutil.which("lint-imports") or str(Path(sys.executable).parent / "lint-imports")
# the child writes UTF-8 and we read UTF-8, whatever the console code page (Windows)
UTF8 = {"env": {**os.environ, "PYTHONUTF8": "1"}, "encoding": "utf-8", "errors": "replace"}


def test_the_repo_keeps_its_architecture_rules():
    done = subprocess.run([LINT], cwd=ROOT, capture_output=True, **UTF8)
    assert done.returncode == 0, done.stdout[-2000:]


def test_a_provider_sdk_import_outside_transport_fails(tmp_path):
    package = tmp_path / "toy"
    (package / "transport").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "transport" / "__init__.py").write_text("import openai\n", encoding="utf-8")
    (package / "engine.py").write_text("import openai\n", encoding="utf-8")  # the violation
    config = tmp_path / "importlinter.toml"
    config.write_text(
        "[tool.importlinter]\nroot_package = 'toy'\ninclude_external_packages = true\n\n"
        "[[tool.importlinter.contracts]]\nname = 'sdk only in transport'\ntype = 'forbidden'\n"
        "source_modules = ['toy']\nforbidden_modules = ['openai']\n"
        "ignore_imports = ['toy.transport -> openai']\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        [LINT, "--config", str(config)], cwd=tmp_path, capture_output=True, **UTF8
    )
    assert done.returncode != 0
    assert "toy.engine -> openai" in done.stdout
