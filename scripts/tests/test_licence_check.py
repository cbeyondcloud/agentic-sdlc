"""The licence allow-list check (P0-03; owner decision DF-83 for Unlicense/CC0)."""

import sys
from email.message import Message
from importlib import metadata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from licence_check import check, classify  # noqa: E402


@pytest.mark.parametrize(
    "expression",
    [
        "MIT",
        "Apache-2.0",
        "BSD-3-Clause",
        "BSD 2-Clause License",
        "ISC",
        "PostgreSQL",
        "Apache-2.0 OR MIT",
        "MIT AND BSD-3-Clause",
        "Unlicense",
        "CC0-1.0",
        "PSF-2.0",
    ],
)
def test_allowed_licences(expression):
    assert classify(expression, []) == "allowed"


@pytest.mark.parametrize(
    "expression",
    [
        "GPL-3.0-or-later",
        "AGPL-3.0",
        "LGPL-2.1",
        "SSPL-1.0",
        "BUSL-1.1",
        "MPL-2.0",
        "Proprietary",
        "MIT AND GPL-2.0",
    ],
)
def test_denied_licences(expression):
    assert classify(expression, []) == "denied"


def test_a_choice_with_one_allowed_option_is_allowed():
    assert classify("GPL-2.0 OR MIT", []) == "allowed"


def test_classifiers_decide_when_the_expression_is_empty():
    assert classify("", ["License :: OSI Approved :: BSD License"]) == "allowed"
    assert (
        classify("", ["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"])
        == "denied"
    )


def test_nothing_known_is_unknown():
    assert classify("", []) == "unknown"
    assert classify("Some Custom Licence", []) == "unknown"


def test_unclear_free_text_falls_back_to_classifiers():
    bsd_apache = [
        "License :: OSI Approved :: BSD License",
        "License :: OSI Approved :: Apache Software License",
    ]
    assert classify("Dual License", bsd_apache) == "allowed"


def test_a_clear_denial_is_not_rescued_by_classifiers():
    assert classify("GPL-3.0-only", ["License :: OSI Approved :: MIT License"]) == "denied"


class _FakeDist:
    def __init__(self, licence: str):
        self.version = "1.0"
        self.metadata = Message()
        self.metadata["License-Expression"] = licence


def _lock(tmp_path, *names):
    registry = '{ registry = "https://pypi.org/simple" }'
    body = "".join(
        f'[[package]]\nname = "{n}"\nversion = "1.0"\nsource = {registry}\n\n' for n in names
    )
    body += '[[package]]\nname = "ours"\nversion = "0.1"\nsource = { editable = "core" }\n'
    path = tmp_path / "uv.lock"
    path.write_text(body, encoding="utf-8")
    return path


def _dists(**licences):
    def distribution(name):
        if name not in licences:
            raise metadata.PackageNotFoundError(name)
        return _FakeDist(licences[name])

    return distribution


def test_a_gpl_dependency_fails_the_check(tmp_path):
    lock = _lock(tmp_path, "good", "copyleft")
    failures = check(lock, tmp_path / "none.toml", _dists(good="MIT", copyleft="GPL-3.0-only"))
    assert failures == ["copyleft 1.0: denied (GPL-3.0-only)"]


def test_an_exception_with_a_reason_lets_it_pass(tmp_path):
    lock = _lock(tmp_path, "copyleft", "elsewhere")
    allowed = tmp_path / "exceptions.toml"
    allowed.write_text('[allow]\ncopyleft = "legal approved"\n', encoding="utf-8")
    assert check(lock, allowed, _dists(copyleft="GPL-3.0-only")) == []  # elsewhere: other OS


def test_a_stale_exception_fails(tmp_path):
    allowed = tmp_path / "exceptions.toml"
    allowed.write_text('[allow]\ngone = "was needed once"\n', encoding="utf-8")
    failures = check(_lock(tmp_path, "good"), allowed, _dists(good="MIT"))
    assert failures == ["gone: listed in exceptions.toml but no longer locked"]


def test_workspace_packages_are_not_checked(tmp_path):
    assert check(_lock(tmp_path), tmp_path / "none.toml", _dists()) == []
