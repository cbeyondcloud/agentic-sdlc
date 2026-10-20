"""Hash-verified tool fetcher over Zyroflo's `tools/third_party.py` (M1-51; D34: reuse).

`agentic init` fetches user-space tools (gitleaks, osv-scanner, ast-grep, ...) from the
org's internal mirror, each pinned by version and sha256 per platform in org policy
(`tool-pins.yaml`). Zyroflo's third_party module supplies the platform key
(`current_platform`, `PLATFORMS`), the on-disk name (`binary_name`) and the archive
extraction (`extract_binary`: zip, tarball or a bare binary). Around them this module
adds what the framework needs:

  * the mirror: `<tool_mirror_url>/<artefact file>`, https only;
  * the cache: `<cache_root>/<tool>/<version>/<platform>/<binary>`, shared across repos,
    with `.verified.json` recording the asset and installed digests. A cached binary
    whose bytes still match its record is used with no network at all (offline works);
    one that changed, or was planted with no record, is fetched again, verified and
    reported (`replaced`);
  * a checksum mismatch writes nothing (`refused`); an unreachable mirror with no cache
    is `failed`, naming the host - never a silent "no tool";
  * Windows: an artefact the org manifest does not mark `signed` is refused and never
    downloaded - no unsigned native binary is installed on a Windows laptop;
  * `record("init.tool_fetched", fields)` for each download, for the audit trail.

The install is atomic: the binary is extracted beside its destination, then renamed into
place. Nothing here runs a fetched binary.
"""

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote, urlsplit

Status = Literal["fetched", "replaced", "cached", "refused", "unavailable", "failed"]
Record = Callable[[str, dict[str, object]], None]
RECORD_NAME = ".verified.json"

#: Zyroflo's platform keys (`<os>-<arch>`), the keys of a pin's `assets`.
PLATFORMS: tuple[str, ...] = (
    "windows-x86_64",
    "linux-x86_64",
    "linux-aarch64",
    "darwin-x86_64",
    "darwin-aarch64",
)


class ToolFetchError(Exception):
    """The fetcher cannot be set up (a bad mirror URL, an unknown platform)."""


class ArtefactLike(Protocol):
    file: str
    sha256: str
    signed: bool


class PinLike(Protocol):
    version: str
    binary: str

    @property
    def assets(self) -> Mapping[str, ArtefactLike]: ...


@dataclass(frozen=True)
class FetchResult:
    name: str
    version: str
    platform: str
    status: Status
    path: Path | None  # the verified binary, when ok
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("fetched", "replaced", "cached")


def _zyroflo():
    # Imported on first use: the module reconfigures stdout/stderr to UTF-8 on import
    # (idempotent, never fatal), which only a fetch needs.
    from tools import third_party

    return third_party


def current_platform() -> str:
    """This machine's platform key; ToolFetchError for one Zyroflo does not know."""
    third_party = _zyroflo()
    try:
        return third_party.current_platform()
    except third_party.UnsupportedPlatform as error:
        raise ToolFetchError(str(error).splitlines()[0]) from error


def windows_refusal(platform: str, artefact: ArtefactLike) -> str | None:
    """Why this artefact may not be installed on `platform`, or None."""
    if platform.startswith("windows") and not artefact.signed:
        return (
            "refused: the org manifest does not mark this Windows artefact as signed, and "
            "no unsigned native binary is installed on Windows"
        )
    return None


class ToolFetcher:
    def __init__(
        self,
        cache_root: Path,
        mirror_url: str,
        *,
        download: Callable[[str], bytes],
        platform: str | None = None,
        record: Record | None = None,
    ) -> None:
        if urlsplit(mirror_url).scheme != "https":
            raise ToolFetchError(f"the tool mirror must be an https URL, not {mirror_url!r}")
        self.cache_root, self.mirror_url = Path(cache_root), mirror_url.rstrip("/")
        self.platform = platform or current_platform()
        if self.platform not in PLATFORMS:
            raise ToolFetchError(f"unknown platform {self.platform!r}")
        self._download, self._record = download, record or (lambda event, fields: None)

    def planned(self, name: str, pin: PinLike) -> tuple[str, Path]:
        """(url, destination) of the tool on this platform, without fetching anything."""
        artefact = pin.assets[self.platform]
        return self._url(artefact), self._destination(name, pin)

    def cached(self, name: str, pin: PinLike) -> Path | None:
        """The verified cached binary for this platform, or None. Never downloads."""
        artefact = pin.assets.get(self.platform)
        if artefact is None:
            return None
        dest = self._destination(name, pin)
        return dest if self._cached_state(dest, artefact.sha256) == "verified" else None

    def fetch(self, name: str, pin: PinLike) -> FetchResult:
        def result(status: Status, path: Path | None = None, detail: str = "") -> FetchResult:
            return FetchResult(name, pin.version, self.platform, status, path, detail)

        artefact = pin.assets.get(self.platform)
        if artefact is None:
            declared = ", ".join(sorted(pin.assets)) or "none"
            return result("unavailable", detail=f"no artefact for {self.platform} ({declared})")
        if why := windows_refusal(self.platform, artefact):
            return result("refused", detail=why)

        dest = self._destination(name, pin)
        previous = self._cached_state(dest, artefact.sha256)
        if previous == "verified":
            return result("cached", dest)

        url = self._url(artefact)
        try:
            blob = self._download(url)
        except OSError as error:
            host = urlsplit(url).hostname or url
            return result(
                "failed", detail=f"could not reach {host} ({type(error).__name__}); no cache"
            )
        found = hashlib.sha256(blob).hexdigest()
        if found != artefact.sha256:
            return result(
                "refused",
                detail=f"checksum mismatch: pinned {artefact.sha256[:12]}..., got {found[:12]}...",
            )
        try:
            self._install(blob, artefact, pin, dest)
        except (ToolFetchError, OSError) as error:
            return result("failed", detail=str(error))

        fields: dict[str, object] = {
            "tool": name,
            "version": pin.version,
            "platform": self.platform,
            "sha256": artefact.sha256,
        }
        if previous != "absent":
            fields["replaced"] = previous
        self._record("init.tool_fetched", fields)
        return result("fetched" if previous == "absent" else "replaced", dest)

    # --- internals ---

    def _url(self, artefact: ArtefactLike) -> str:
        return f"{self.mirror_url}/{quote(artefact.file.replace(chr(92), '/'), safe='/')}"

    def _destination(self, name: str, pin: PinLike) -> Path:
        member = _zyroflo().binary_name({"binary": pin.binary}, self.platform)
        return self.cache_root / name / pin.version / self.platform / member

    def _cached_state(self, dest: Path, asset_sha: str) -> Literal["absent", "verified", "changed"]:
        if not dest.exists():
            return "absent"
        try:
            record = json.loads((dest.parent / RECORD_NAME).read_text(encoding="utf-8"))
            installed = hashlib.sha256(dest.read_bytes()).hexdigest()
        except (OSError, ValueError):
            return "changed"  # no record: never vouched for, so fetched and verified again
        if record.get("asset_sha256") == asset_sha and record.get("installed_sha256") == installed:
            return "verified"
        return "changed"

    def _install(self, blob: bytes, artefact: ArtefactLike, pin: PinLike, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".fetch-") as scratch:
            staged = Path(scratch) / dest.name
            try:
                _zyroflo().extract_binary(blob, Path(artefact.file).name, dest.name, staged)
            except SystemExit as error:  # Zyroflo's CLI helpers stop with a message
                raise ToolFetchError(f"{pin.binary}: {error}") from None
            installed = hashlib.sha256(staged.read_bytes()).hexdigest()
            record = dest.parent / RECORD_NAME
            if dest.exists():
                _remove_file(dest)
            os.replace(staged, dest)
        text = json.dumps(
            {"asset_sha256": artefact.sha256, "installed_sha256": installed}, indent=2
        )
        record.write_text(text + "\n", encoding="utf-8", newline="\n")


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:  # read-only on Windows
        os.chmod(path, 0o600)
        path.unlink()
    except IsADirectoryError:
        shutil.rmtree(path)
