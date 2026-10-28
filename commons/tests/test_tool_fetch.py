"""Tests for the hash-verified tool fetcher over Zyroflo's third_party helpers (M1-51).

No fetched file is ever executed: the "binaries" are a few bytes of text."""

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sdlc_commons.tool_fetch import (
    PLATFORMS,
    FetchResult,
    ToolFetcher,
    ToolFetchError,
    current_platform,
    windows_refusal,
)

MIRROR = "https://mirror.example/agentic"
LINUX = "linux-x86_64"
WINDOWS = "windows-x86_64"
PAYLOAD = b"not really a binary\n"


@dataclass(frozen=True)
class Artefact:
    file: str
    sha256: str
    signed: bool = False


@dataclass(frozen=True)
class Pin:
    version: str
    binary: str
    assets: dict = field(default_factory=dict)


def tarball(member: str, data: bytes) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Mirror:
    """A fake mirror: URL -> bytes; counts downloads; offline raises like a dead network."""

    def __init__(self, files: dict[str, bytes], offline: bool = False) -> None:
        self.files, self.offline, self.asked = files, offline, []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        if self.offline or url not in self.files:
            raise OSError("Name or service not known")
        return self.files[url]


BLOB = tarball("pkg/mytool", PAYLOAD)
FILE = "mytool/1.0/mytool_linux_x64.tar.gz"
PIN = Pin("1.0", "mytool", {LINUX: Artefact(FILE, sha(BLOB))})


def fetcher(tmp_path: Path, mirror: Mirror, platform: str = LINUX, events=None) -> ToolFetcher:
    record = (lambda event, fields: events.append((event, fields))) if events is not None else None
    return ToolFetcher(
        tmp_path / "tools", MIRROR, platform=platform, download=mirror, record=record
    )


def test_a_good_artefact_is_fetched_verified_and_recorded(tmp_path: Path):
    events: list = []
    result = fetcher(tmp_path, Mirror({f"{MIRROR}/{FILE}": BLOB}), events=events).fetch(
        "mytool", PIN
    )
    assert isinstance(result, FetchResult) and result.status == "fetched" and result.ok
    assert result.path == tmp_path / "tools" / "mytool" / "1.0" / LINUX / "mytool"
    assert result.path.read_bytes() == PAYLOAD
    assert events == [
        (
            "init.tool_fetched",
            {"tool": "mytool", "version": "1.0", "platform": LINUX, "sha256": sha(BLOB)},
        )
    ]


def test_a_bad_hash_is_refused_and_nothing_written(tmp_path: Path):
    tampered = tarball("pkg/mytool", b"something else\n")
    result = fetcher(tmp_path, Mirror({f"{MIRROR}/{FILE}": tampered})).fetch("mytool", PIN)
    assert result.status == "refused" and not result.ok and "checksum" in result.detail
    assert not (tmp_path / "tools").exists() or not any((tmp_path / "tools").rglob("mytool"))


def test_offline_uses_the_verified_cache(tmp_path: Path):
    fetcher(tmp_path, Mirror({f"{MIRROR}/{FILE}": BLOB})).fetch("mytool", PIN)
    offline = Mirror({}, offline=True)
    result = fetcher(tmp_path, offline).fetch("mytool", PIN)
    assert result.status == "cached" and result.ok and offline.asked == []


def test_offline_without_a_cache_fails_naming_the_host(tmp_path: Path):
    result = fetcher(tmp_path, Mirror({}, offline=True)).fetch("mytool", PIN)
    assert result.status == "failed" and "mirror.example" in result.detail


def test_a_changed_cached_binary_is_replaced_and_reported(tmp_path: Path):
    mirror = Mirror({f"{MIRROR}/{FILE}": BLOB})
    first = fetcher(tmp_path, mirror).fetch("mytool", PIN)
    first.path.write_bytes(b"planted\n")
    events: list = []
    again = fetcher(tmp_path, mirror, events=events).fetch("mytool", PIN)
    assert again.status == "replaced" and again.path.read_bytes() == PAYLOAD
    assert events[0][0] == "init.tool_fetched" and events[0][1]["replaced"] == "changed"


def test_a_planted_binary_without_a_record_is_not_trusted(tmp_path: Path):
    dest = tmp_path / "tools" / "mytool" / "1.0" / LINUX / "mytool"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"planted before the first fetch\n")
    result = fetcher(tmp_path, Mirror({f"{MIRROR}/{FILE}": BLOB})).fetch("mytool", PIN)
    assert result.status == "replaced" and dest.read_bytes() == PAYLOAD


def test_an_unsigned_windows_artefact_is_refused_and_never_downloaded(tmp_path: Path):
    pin = Pin("1.0", "mytool", {WINDOWS: Artefact("mytool/1.0/w.zip", "b" * 64)})
    mirror = Mirror({})
    result = fetcher(tmp_path, mirror, platform=WINDOWS).fetch("mytool", pin)
    assert result.status == "refused" and "signed" in result.detail
    assert mirror.asked == [] and not (tmp_path / "tools").exists()


def test_the_signed_rule_applies_to_windows_only():
    assert windows_refusal(WINDOWS, Artefact("w.zip", "b" * 64, signed=True)) is None
    assert windows_refusal(LINUX, Artefact("l.tgz", "b" * 64)) is None
    assert windows_refusal(WINDOWS, Artefact("w.zip", "b" * 64)) is not None


def test_a_platform_without_an_asset_is_unavailable(tmp_path: Path):
    result = fetcher(tmp_path, Mirror({}), platform="darwin-aarch64").fetch("mytool", PIN)
    assert result.status == "unavailable" and "darwin-aarch64" in result.detail


def test_a_member_missing_from_the_archive_fails_cleanly(tmp_path: Path):
    blob = tarball("pkg/other", PAYLOAD)
    pin = Pin("1.0", "mytool", {LINUX: Artefact(FILE, sha(blob))})
    result = fetcher(tmp_path, Mirror({f"{MIRROR}/{FILE}": blob})).fetch("mytool", pin)
    assert result.status == "failed" and "mytool" in result.detail


def test_a_bare_binary_asset_is_written_as_is(tmp_path: Path):
    pin = Pin("2.0", "bare", {LINUX: Artefact("bare/2.0/bare", sha(PAYLOAD))})
    mirror = Mirror({f"{MIRROR}/bare/2.0/bare": PAYLOAD})
    assert fetcher(tmp_path, mirror).fetch("bare", pin).path.read_bytes() == PAYLOAD


def test_the_mirror_must_be_https(tmp_path: Path):
    with pytest.raises(ToolFetchError, match="https"):
        ToolFetcher(tmp_path, "http://mirror.example", platform=LINUX, download=Mirror({}))


def test_planned_names_the_url_and_destination_without_fetching(tmp_path: Path):
    mirror = Mirror({})
    url, dest = fetcher(tmp_path, mirror).planned("mytool", PIN)
    assert url == f"{MIRROR}/{FILE}" and dest.name == "mytool" and mirror.asked == []


def test_cached_says_whether_a_verified_copy_exists_without_fetching(tmp_path: Path):
    mirror = Mirror({f"{MIRROR}/{FILE}": BLOB})
    assert fetcher(tmp_path, mirror).cached("mytool", PIN) is None
    path = fetcher(tmp_path, mirror).fetch("mytool", PIN).path
    asked = len(mirror.asked)
    assert fetcher(tmp_path, mirror).cached("mytool", PIN) == path and len(mirror.asked) == asked
    path.write_bytes(b"changed\n")
    assert fetcher(tmp_path, mirror).cached("mytool", PIN) is None


def test_the_record_file_is_json_with_lf(tmp_path: Path):
    result = fetcher(tmp_path, Mirror({f"{MIRROR}/{FILE}": BLOB})).fetch("mytool", PIN)
    record = result.path.parent / ".verified.json"
    assert b"\r\n" not in record.read_bytes()
    assert json.loads(record.read_text(encoding="utf-8"))["asset_sha256"] == sha(BLOB)


def test_this_machine_has_a_known_platform():
    assert current_platform() in PLATFORMS


def test_the_platform_keys_are_zyroflo_s():
    from tools import third_party

    assert PLATFORMS == tuple(third_party.PLATFORMS)


@pytest.mark.parametrize(
    ("name", "version", "binary"),
    [
        ("../../..", "1.0", "mytool"),
        ("..", "1.0", "mytool"),
        ("mytool", "..", "mytool"),
        ("mytool", "../../x", "mytool"),
        ("/abs", "1.0", "mytool"),
        ("C:", "1.0", "mytool"),
        ("mytool", "1.0", ".."),
    ],
)
def test_a_pin_that_would_leave_the_cache_is_refused(
    tmp_path: Path, name: str, version: str, binary: str
):
    """Defence in depth under the policy model's own check: the cache path is built only
    from plain names, so nothing is downloaded or written outside the cache."""
    cache = tmp_path / "a" / "b" / "tools"
    pin = Pin(version, binary, {LINUX: Artefact(FILE, sha(BLOB))})
    mirror = Mirror({f"{MIRROR}/{FILE}": BLOB})
    tool_fetcher = ToolFetcher(cache, MIRROR, platform=LINUX, download=mirror)
    result = tool_fetcher.fetch(name, pin)
    assert result.status == "refused" and "plain name" in result.detail
    assert mirror.asked == [] and not any(tmp_path.rglob("*mytool*"))
    assert tool_fetcher.cached(name, pin) is None
    with pytest.raises(ToolFetchError, match="plain name"):
        tool_fetcher.planned(name, pin)


@pytest.mark.parametrize(
    ("file", "blob"),
    [
        ("mytool/1.0/mytool.zip", b"this is not a zip\n"),
        ("mytool/1.0/mytool.tar.gz", b"this is not a tarball\n"),
        ("mytool/1.0/mytool.tar.gz", BLOB[: len(BLOB) // 2]),
    ],
    ids=["not-a-zip", "not-a-tarball", "truncated-tarball"],
)
def test_a_pinned_file_that_is_not_a_valid_archive_fails_cleanly(
    tmp_path: Path, file: str, blob: bytes
):
    pin = Pin("1.0", "mytool", {LINUX: Artefact(file, sha(blob))})
    result = fetcher(tmp_path, Mirror({f"{MIRROR}/{file}": blob})).fetch("mytool", pin)
    assert result.status == "failed" and not result.ok
    assert "mytool" in result.detail and "not a valid archive" in result.detail
    assert not any(path.is_file() for path in (tmp_path / "tools").rglob("mytool"))
