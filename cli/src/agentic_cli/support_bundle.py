"""`agentic support-bundle` (M3-09, spec §20): one zip a developer sends the platform team
when something does not work - with nothing secret or personal in it.

What goes in (and nothing else - no ledger, handoff, prompt, diff or source file):

  * `manifest.json`  - versions (agentic, Python, OS, git), when it was made, its contents;
  * `doctor.json`    - `agentic doctor`'s report;
  * `config/user-config.yaml`, `config/repo-config.yaml` - with every secret-named key's
    value replaced by `<removed>`;
  * `init.json`      - what `agentic init` found in this repository;
  * `runs/<run>/summary.json` - the most recent runs' summaries (`agentic status <run>`);
  * `logs/<run>/<step>.jsonl` - the last lines of the most recent runs' step logs.

Every text goes through three passes before it is written:

  1. the commons redactor (`sdlc_commons.redaction`, Zyroflo's masking): the values of the
     environment's secret-named variables, the stored credentials (gateway key, control
     plane and hosting tokens) and credential shapes;
  2. the M2-49 publish scan in `mask` mode, with no field exempt: every secret detector and
     every personal-data kind (an email, a phone or card number) becomes `masked-<kind>`;
  3. an HTTP authorization header's credentials become `<removed>`, and the home folder's
     path - in any case, with either slash - becomes `~` (it carries the account name).

The finished texts are then scanned once more, by both. Anything still found stops the
command and nothing is written (fail closed): a zip is written to a temporary file beside
the output and renamed into place only when every member passed.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic.auth import TOKENLEAN_KEY, CredentialError, Credentials
from agentic.engine.state import RunState, RunStateError, RunStore
from agentic.sandbox.process import SECRET_NAME
from agentic.security.pii import PiiDetector
from agentic.security.publish_scan import PublishGuard
from agentic.security.secret_scan import SecretScanner
from agentic.telemetry.logs import redact_record
from agentic.yamlio import YamlFileError, dump_yaml, load_yaml, read_text
from agentic_cli import control_plane
from agentic_cli.doctor import DoctorReport, versions
from agentic_cli.driving import summary
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.workspace import INIT_STATE, REPO_CONFIG, Workspace
from sdlc_commons.redaction import redactor

SUMMARY_RUNS = 10  # run summaries included, newest first
LOG_RUNS = 3  # runs whose step logs are included
LOG_LINES = 200  # last lines of each step log
REMOVED = "<removed>"
NOTE = (
    "Made by `agentic support-bundle`: redacted (secret values, credential shapes and "
    "personal data masked; secret-named settings removed) and scanned again before writing. "
    "No ledger, handoff, prompt, diff or source file is included."
)
#: Stored credentials whose values are masked wherever they appear.
CREDENTIALS = (TOKENLEAN_KEY, control_plane.CONTROL_PLANE_TOKEN, "github_token")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _home() -> Path:
    return Path.home()


def default_name(now: datetime) -> str:
    return f"agentic-support-{now.strftime('%Y%m%dT%H%M%SZ')}.zip"


def build(
    output: Path,
    *,
    ws: Workspace | None,
    home: Path,
    env: Mapping[str, str],
    report: DoctorReport,
) -> list[str]:
    """Write the bundle to `output` (refused if it exists); returns its member names."""
    output = Path(output)
    if output.exists():
        raise CliError(
            f"{output} already exists: nothing was written",
            ExitCode.REFUSED,
            hint="choose another --output",
        )
    clean = _cleaner(env)
    texts: dict[str, str] = {}
    texts["doctor.json"] = clean.text(json.dumps(report.to_json(), indent=2))
    texts["config/user-config.yaml"] = clean.text(_config_text(home / "config.yaml"))
    if ws is not None:
        texts["config/repo-config.yaml"] = clean.text(_config_text(ws.repo / REPO_CONFIG))
        init = ws.repo / INIT_STATE
        if init.is_file():
            texts["init.json"] = clean.text(_read(init))
        texts.update(_runs(ws, clean))
    contents = ["manifest.json", *texts]
    manifest = {
        "created_at": utc_now().isoformat(timespec="seconds"),
        "versions": versions(),
        "contents": contents,
        "note": NOTE,
    }
    texts = {"manifest.json": clean.text(json.dumps(manifest, indent=2)), **texts}
    for name, text in texts.items():  # the last pass: anything left stops everything
        if found := _leftover(text, clean.check):
            raise CliError(
                f"the bundle's {name} still holds {found} after redaction: nothing was written",
                ExitCode.ERROR,
                hint="please report this to the platform team without the bundle",
            )
    _write(output, texts)
    return list(texts)


# ── redaction ─────────────────────────────────────────────────────────────────


#: An HTTP authorization header's credentials (Basic is base64 of user:password, a shape
#: no secret detector knows): the scheme stays, the credentials go.
AUTHORIZATION = re.compile(
    r"\b((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?"
    r"(?:(?:basic|bearer|digest|token|negotiate|ntlm)\s+)?)[A-Za-z0-9+/=._~-]{6,}",
    re.IGNORECASE,
)
#: Between the parts of a path: either slash, JSON-escaped backslashes included.
_SEPARATOR = r"(?:\\|/)+"


def home_pattern(home: Path) -> re.Pattern[str] | None:
    """Every spelling of the home folder: either slash style, JSON-escaped, any case
    (Windows and macOS paths are case-insensitive, so tools write them either way)."""
    parts = [part for part in re.split(r"[\\/]+", str(home)) if part]
    if not parts:
        return None
    lead = _SEPARATOR if str(home)[:1] in ("/", "\\") else ""
    body = _SEPARATOR.join(re.escape(part) for part in parts)
    return re.compile(rf"{lead}{body}(?![\w.-])", re.IGNORECASE)


class _Cleaner:
    def __init__(self, redact: Callable[[str], str], guard: PublishGuard, home: Path) -> None:
        self.redact, self.guard = redact, guard
        self.home = home_pattern(home)

    def text(self, text: str) -> str:
        """Each line on its own: the scan's time limit applies per line, not per file."""
        return "\n".join(self._line(line) for line in text.split("\n"))

    def _line(self, line: str) -> str:
        masked = self.guard.screen("support_bundle", {"text": self.redact(line)})["text"]
        masked = self.redact(masked)  # a scan marker can complete a shape the redactor masks
        masked = AUTHORIZATION.sub(rf"\1{REMOVED}", masked)
        return self.home.sub("~", masked) if self.home is not None else masked

    def check(self, text: str) -> str | None:
        """What the redactor or the scan still finds in `text`, as kinds, or None."""
        for line in text.split("\n"):
            if self.redact(line) != line:
                return "a known secret value"
            if found := self.guard.findings({"text": line}):
                return ", ".join(sorted(found))
            if AUTHORIZATION.search(line):
                return "an authorization header's credentials"
            if self.home is not None and self.home.search(line):
                return "the home folder's path"
        return None


def _leftover(text: str, check: Callable[[str], str | None]) -> str | None:
    return check(text)


def _cleaner(env: Mapping[str, str]) -> _Cleaner:
    known = []
    for name in CREDENTIALS:
        try:
            found = Credentials(environ=env).find(name)
        except CredentialError:
            found = None
        if found is not None:
            known.append(found.value)
    guard = PublishGuard(
        SecretScanner("mask", on_event=lambda event: None),
        PiiDetector(),
        action="mask",
        identity_fields=(),  # unlike run events, no field names people the audit is about
        on_event=lambda event: None,
    )
    return _Cleaner(redactor(env, extra=known), guard, _home())


def _config_text(path: Path) -> str:
    """A config file with every secret-named key's value removed; `{}` when absent."""
    try:
        data = load_yaml(read_text(path))[0] if path.is_file() else {}
    except (OSError, YamlFileError) as error:
        return f"# {path.name} could not be read: {type(error).__name__}\n"
    if not isinstance(data, dict):
        return f"# {path.name} is not a mapping of settings\n"
    return dump_yaml(_without_secrets(data))


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REMOVED if SECRET_NAME.search(str(key)) else _without_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


# ── runs ──────────────────────────────────────────────────────────────────────


def _runs(ws: Workspace, clean: _Cleaner) -> dict[str, str]:
    store = RunStore(ws.runs_root)
    states: list[RunState] = []
    root = ws.runs_root
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        if folder.name.startswith(".") or not (folder / "run.json").is_file():
            continue
        try:
            states.append(store.load(folder.name))
        except RunStateError:
            continue
    states.sort(key=lambda s: s.updated_at, reverse=True)
    texts: dict[str, str] = {}
    for state in states[:SUMMARY_RUNS]:
        data = summary(state, clean.redact)
        data["end_reason"] = state.end_reason
        data["ended_by"] = state.ended_by
        texts[f"runs/{state.run_id}/summary.json"] = clean.text(json.dumps(data, indent=2))
    for state in states[:LOG_RUNS]:
        logs = store.path(state.run_id).parent / "logs"
        for path in sorted(logs.glob("*.jsonl")) if logs.is_dir() else []:
            texts[f"logs/{state.run_id}/{path.name}"] = _log_tail(path, clean)
    return texts


def _log_tail(path: Path, clean: _Cleaner) -> str:
    try:
        lines = path.read_bytes().decode("utf-8", errors="replace").splitlines()
    except OSError as error:
        return json.dumps({"error": f"cannot be read: {type(error).__name__}"}) + "\n"
    kept = []
    for line in lines[-LOG_LINES:]:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a half-written last line
        if isinstance(record, dict):
            record = redact_record(record, clean.redact)
            kept.append(clean.text(json.dumps(record, ensure_ascii=False)))
    return "".join(line + "\n" for line in kept)


# ── writing ───────────────────────────────────────────────────────────────────


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _write(output: Path, texts: Mapping[str, str]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    try:
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, text in texts.items():
                archive.writestr(name, text.encode("utf-8"))
        os.replace(staged, output)
    finally:
        staged.unlink(missing_ok=True)
