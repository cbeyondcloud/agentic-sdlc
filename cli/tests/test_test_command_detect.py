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
