"""The repo's test command, read from its manifests (DF-359, owner decision 2026-11-07):
one row per manifest, no command for a repo that has none."""

import json
from pathlib import Path

import pytest

from agentic_cli.test_commands import RepoTestCommand, detect

NPM_PLACEHOLDER = 'echo "Error: no test specified" && exit 1'  # what `npm init` writes


def files(root: Path, **named: str) -> Path:
    for rel, text in named.items():
        path = root / rel.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def commands(root: Path) -> list[tuple[str, str]]:
    return [(c.command, c.allow) for c in detect(root)]


def test_an_empty_repo_has_no_test_command(tmp_path):
    files(tmp_path, **{"README.md": "hello\n"})
    assert detect(tmp_path) == []


@pytest.mark.parametrize(
    ("named", "expected"),
    [
        ({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n"}, ("pytest", "pytest")),
        ({"pytest.ini": "[pytest]\n"}, ("pytest", "pytest")),
        ({"setup.cfg": "[tool:pytest]\ntestpaths = tests\n"}, ("pytest", "pytest")),
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\n", "uv.lock": "version = 1\n"},
            ("uv run pytest", "uv run pytest"),
        ),
        ({"package.json": '{"scripts": {"test": "vitest"}}'}, ("npm test", "npm test")),
        (
            {"package.json": '{"scripts": {"test": "jest"}}', "yarn.lock": ""},
            ("yarn test", "yarn test"),
        ),
        (
            {"package.json": '{"scripts": {"test": "jest"}}', "pnpm-lock.yaml": ""},
            ("pnpm test", "pnpm test"),
        ),
        ({"Makefile": "build:\n\tcc x.c\n\ntest: build\n\t./run\n"}, ("make test", "make test")),
        ({"go.mod": "module example.com/x\n"}, ("go test ./...", "go test")),
        ({"pom.xml": "<project/>\n"}, ("mvn test", "mvn test")),
        ({"pom.xml": "<project/>\n", "mvnw": "#!/bin/sh\n"}, ("./mvnw test", "./mvnw test")),
        ({"pom.xml": "<project/>\n", "mvnw.cmd": "@echo off\n"}, ("./mvnw test", "./mvnw test")),
        ({"build.gradle": "plugins {}\n"}, ("gradle test", "gradle test")),
        (
            {"build.gradle.kts": "plugins {}\n", "gradlew": "#!/bin/sh\n"},
            ("./gradlew test", "./gradlew test"),
        ),
        ({"Cargo.toml": "[package]\nname = 'x'\n"}, ("cargo test", "cargo test")),
    ],
)
def test_each_manifest_names_its_test_command(tmp_path, named, expected):
    files(tmp_path, **named)
    assert commands(tmp_path) == [expected]


def test_the_source_is_the_manifest_it_came_from(tmp_path):
    files(tmp_path, **{"go.mod": "module x\n"})
    assert detect(tmp_path) == [RepoTestCommand("go test ./...", "go test", "go.mod")]


def test_a_pyproject_without_pytest_and_npms_placeholder_script_are_not_test_commands(tmp_path):
    files(
        tmp_path,
        **{
            "pyproject.toml": "[project]\nname = 'x'\n",
            "package.json": json.dumps({"scripts": {"test": NPM_PLACEHOLDER}}),
            "Makefile": "testing:\n\techo no\n",
        },
    )
    assert detect(tmp_path) == []


def test_an_unreadable_manifest_is_skipped_not_raised(tmp_path):
    files(tmp_path, **{"package.json": "{not json", "pyproject.toml": "[broken", "go.mod": "m\n"})
    assert commands(tmp_path) == [("go test ./...", "go test")]


def test_several_manifests_give_several_commands_once_each(tmp_path):
    files(
        tmp_path,
        **{
            "pyproject.toml": "[tool.pytest.ini_options]\n",
            "pytest.ini": "[pytest]\n",
            "package.json": '{"scripts": {"test": "jest"}}',
        },
    )
    assert commands(tmp_path) == [("pytest", "pytest"), ("npm test", "npm test")]


# ── review fixes: more evidence, equivalents, BOMs ───────────────────────────


def options(root: Path) -> list[list[str]]:
    return [[command for command, _ in c.options] for c in detect(root)]


@pytest.mark.parametrize(
    ("named", "source"),
    [
        ({"pyproject.toml": "[dependency-groups]\ndev = ['pytest>=8']\n"}, "pyproject.toml"),
        (
            {"pyproject.toml": "[project.optional-dependencies]\ntest = ['pytest', 'x']\n"},
            "pyproject.toml",
        ),
        (
            {"pyproject.toml": "[tool.poetry.group.dev.dependencies]\npytest = '^8'\n"},
            "pyproject.toml",
        ),
        ({"pyproject.toml": "[tool.poetry.dev-dependencies]\nPyTest = '^7'\n"}, "pyproject.toml"),
        ({"conftest.py": ""}, "conftest.py"),
        ({"tests__test_app.py": "def test_x(): pass\n"}, "tests/"),
    ],
)
def test_a_pytest_dependency_or_test_files_are_python_evidence(tmp_path, named, source):
    files(tmp_path, **named)
    (found,) = detect(tmp_path)
    assert (found.command, found.source) == ("pytest", source)


def test_a_dependency_merely_named_like_pytest_is_not_evidence(tmp_path):
    files(tmp_path, **{"pyproject.toml": "[project]\ndependencies = ['pytestify-lib']\n"})
    assert detect(tmp_path) == []


def test_a_poetry_lock_prefers_poetry_run_pytest(tmp_path):
    files(tmp_path, **{"pytest.ini": "[pytest]\n", "poetry.lock": ""})
    assert commands(tmp_path) == [("poetry run pytest", "poetry run pytest")]


def test_python_equivalents_are_options_of_one_command(tmp_path):
    files(tmp_path, **{"pytest.ini": "[pytest]\n"})
    assert options(tmp_path) == [
        ["pytest", "python -m pytest", "uv run pytest", "poetry run pytest"]
    ]


def test_node_runners_and_their_run_forms_are_options(tmp_path):
    files(tmp_path, **{"package.json": '{"scripts": {"test": "jest"}}', "yarn.lock": ""})
    assert options(tmp_path) == [["yarn test", "yarn run test", "npm test", "npm run test"]]


@pytest.mark.parametrize(
    ("named", "expected"),
    [
        ({"settings.gradle": "rootProject.name = 'x'\n"}, ["gradle test", "./gradlew test"]),
        (
            {"settings.gradle.kts": "\n", "gradlew.bat": "@echo off\n"},
            ["./gradlew test", "gradle test"],
        ),
        ({"pom.xml": "<project/>\n", "mvnw.cmd": "\n"}, ["./mvnw test", "mvn test"]),
        ({"App.sln": "\n"}, ["dotnet test"]),
        ({"src__App__App.csproj": "<Project/>\n"}, ["dotnet test"]),
    ],
)
def test_build_wrappers_and_dotnet(tmp_path, named, expected):
    files(tmp_path, **named)
    assert options(tmp_path) == [expected]


def test_a_bom_and_crlf_package_json_and_pyproject_are_read(tmp_path):
    (tmp_path / "package.json").write_bytes(
        b'\xef\xbb\xbf{\r\n  "scripts": {\r\n    "test": "jest"\r\n  }\r\n}\r\n'
    )
    (tmp_path / "pyproject.toml").write_bytes(b"\xef\xbb\xbf[tool.pytest.ini_options]\r\n")
    assert commands(tmp_path) == [("pytest", "pytest"), ("npm test", "npm test")]


@pytest.mark.parametrize(
    "text",
    [
        '{"name": "x"}',
        '{"scripts": {"build": "tsc"}}',
        json.dumps({"scripts": {"test": NPM_PLACEHOLDER}}),
    ],
)
def test_a_package_json_without_a_real_test_script_has_no_test_command(tmp_path, text):
    files(tmp_path, **{"package.json": text})
    assert detect(tmp_path) == []
