"""Release the agentic-sdlc packages (M3-21): one command on a laptop or in the org's CI.

    uv run python scripts/release.py version [--check | --set X.Y.Z | --bump major|minor|patch|auto]
    uv run python scripts/release.py changelog [--version X.Y.Z] [--write]
    uv run python scripts/release.py build [--out DIR] [--tag vX.Y.Z] [--no-git-deps]
    uv run python scripts/release.py sign [--dir DIR] [--key FILE]
    uv run python scripts/release.py verify [--dir DIR] --allowed-signers FILE --identity ID
    uv run python scripts/release.py publish [--dir DIR] --url URL [--check-url URL] ...
    uv run python scripts/release.py smoke --index-url SIMPLE_URL [--version X.Y.Z]
    uv run python scripts/release.py release ...   # build, sign, verify, publish, smoke

Settings may come from the environment instead of flags (what the workflow template
does): AGENTIC_RELEASE_SIGNING_KEY (a key file) and AGENTIC_RELEASE_SIGNING_KEY_PASSWORD,
AGENTIC_RELEASE_ALLOWED_SIGNERS, AGENTIC_RELEASE_IDENTITY, AGENTIC_RELEASE_INDEX_URL,
AGENTIC_RELEASE_CHECK_URL (the index's simple URL). Index credentials only ever come from
UV_PUBLISH_USERNAME / UV_PUBLISH_PASSWORD or UV_PUBLISH_TOKEN. The runbook is
docs/runbooks/release.md. Exit codes: 0 done, 1 failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from agentic_release import build, changelog, publish, sbom, signing, smoke
from agentic_release import version as versions
from agentic_release.envs import scrubbed_env

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV = {
    "key": "AGENTIC_RELEASE_SIGNING_KEY",
    "key_password": "AGENTIC_RELEASE_SIGNING_KEY_PASSWORD",
    "allowed_signers": "AGENTIC_RELEASE_ALLOWED_SIGNERS",
    "identity": "AGENTIC_RELEASE_IDENTITY",
    "url": "AGENTIC_RELEASE_INDEX_URL",
    "check_url": "AGENTIC_RELEASE_CHECK_URL",
}


class ReleaseError(RuntimeError):
    """A step that cannot go on; its message is shown as it is."""


def _setting(args: argparse.Namespace, name: str, required: bool = True) -> str | None:
    value = getattr(args, name, None) or os.environ.get(ENV[name], "").strip() or None
    if value is None and required:
        flag = "--" + name.replace("_", "-")
        raise ReleaseError(f"{flag} is required (or set {ENV[name]})")
    return value


def _out_dir(args: argparse.Namespace, repo: Path) -> Path:
    if getattr(args, "dir", None):
        return Path(args.dir)
    return repo / "dist" / f"release-{versions.read_version(repo)}"


# --- commands ---


def cmd_version(args: argparse.Namespace, repo: Path) -> None:
    if args.check:
        problems = versions.check_versions(repo)
        if problems:
            raise ReleaseError("the versions disagree:\n  " + "\n  ".join(problems))
    new = args.set
    if args.bump:
        current = versions.read_version(repo)
        part = args.bump
        if part == "auto":
            commits = changelog.commits_since(repo, changelog.last_tag(repo))
            part = changelog.suggest_bump(commits)
        new = versions.bump(current, part)
    if new:
        versions.set_version(repo, new)
        if not args.no_lock:
            # uv lock builds a git-sourced dependency to read it: its backend gets no secret
            done = subprocess.run(
                ["uv", "lock"], cwd=repo, env=scrubbed_env(), capture_output=True, text=True
            )
            if done.returncode != 0:
                raise ReleaseError(f"uv lock failed: {done.stderr.strip()}")
    print(versions.read_version(repo))


def cmd_changelog(args: argparse.Namespace, repo: Path) -> None:
    version = args.version or versions.read_version(repo)
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    section = changelog.render(
        version, day, changelog.commits_since(repo, changelog.last_tag(repo))
    )
    if args.write:
        changelog.prepend(repo / "CHANGELOG.md", section)
    print(section, end="")


def cmd_build(args: argparse.Namespace, repo: Path) -> Path:
    version = versions.read_version(repo)
    if args.tag and args.tag != f"v{version}":
        raise ReleaseError(f"the tag {args.tag} is not v{version}, the version in pyproject.toml")
    out = Path(args.out) if args.out else repo / "dist" / f"release-{version}"
    files = build.build_packages(repo, out, allow_dirty=args.allow_dirty)
    if not args.no_git_deps:
        with tempfile.TemporaryDirectory(prefix="agentic-release-git-") as work:
            files += build.build_git_sources(repo / "uv.lock", out, Path(work))
    epoch = build.commit_epoch(repo)
    bom = sbom.export_sbom(repo, version, epoch)
    _write(out / f"agentic-sdlc-{version}.cdx.json", sbom.dumps(bom))
    _write(out / f"third-party-notices-{version}.md", sbom.notices(bom, repo))
    _write(out / f"constraints-{version}.txt", sbom.constraints(repo))
    manifest = {
        "name": sbom.RELEASE_NAME,
        "version": version,
        "commit": build._git(repo, "rev-parse", "HEAD").strip(),
        "source_date_epoch": epoch,
        "dirty": build.dirty(repo),
        "uv": subprocess.run(["uv", "--version"], capture_output=True, text=True).stdout.strip(),
        "files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(out.iterdir(), key=lambda p: p.name)
        },
    }
    _write(out / "release.json", json.dumps(manifest, indent=2) + "\n")
    print(f"built {len(manifest['files'])} files into {out}")
    return out


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _allowed(args: argparse.Namespace) -> tuple[dict, str]:
    path = Path(_setting(args, "allowed_signers"))
    identity = _setting(args, "identity")
    return signing.parse_allowed(path.read_text(encoding="utf-8")), identity


def cmd_sign(args: argparse.Namespace, repo: Path) -> None:
    password = os.environ.get(ENV["key_password"])
    key = signing.load_private_key(
        Path(_setting(args, "key")), password.encode() if password else None
    )
    folder = _out_dir(args, repo)
    written = signing.sign_dir(folder, key)
    print(f"signed {len(written)} files in {folder}")


def cmd_verify(args: argparse.Namespace, repo: Path) -> None:
    allowed, identity = _allowed(args)
    folder = _out_dir(args, repo)
    names = signing.verify_dir(folder, allowed, identity)
    print(f"verified {len(names)} files in {folder}, signed by {identity}")


def cmd_publish(args: argparse.Namespace, repo: Path) -> None:
    url = _setting(args, "url")
    allowed, identity = _allowed(args)
    folder = _out_dir(args, repo)
    sent = publish.publish(
        folder,
        url,
        allowed=allowed,
        identity=identity,
        check_url=_setting(args, "check_url", False),
    )
    print(f"published {len(sent)} files to {url}")


def cmd_smoke(args: argparse.Namespace, repo: Path) -> None:
    version = args.version or versions.read_version(repo)
    index = args.index_url or _setting(args, "check_url")
    with tempfile.TemporaryDirectory(prefix="agentic-release-smoke-") as work:
        result = smoke.smoke(
            index, version, Path(work), deps_from_lock=repo if args.deps_from_lock else None
        )
    print(f"smoke: agentic {result['version']} installed from {index}; the dry run passed")


def cmd_release(args: argparse.Namespace, repo: Path) -> None:
    out = cmd_build(args, repo)
    args.dir = str(out)
    cmd_sign(args, repo)
    cmd_verify(args, repo)
    if args.no_publish:
        print("not published (--no-publish)")
        return
    cmd_publish(args, repo)
    if args.index_url or _setting(args, "check_url", False):
        cmd_smoke(args, repo)


# --- argument parsing ---


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("version", help="show, check, set or bump the release version")
    v.add_argument("--check", action="store_true", help="fail when any version disagrees")
    group = v.add_mutually_exclusive_group()
    group.add_argument("--set", help="the new version (MAJOR.MINOR.PATCH[aN|bN|rcN])")
    group.add_argument("--bump", choices=[*versions.PARTS, "auto"], help="auto: from commits")
    v.add_argument("--no-lock", action="store_true", help="do not run uv lock afterwards")

    c = sub.add_parser("changelog", help="the changelog section since the last v* tag")
    c.add_argument("--version", help="the version to head the section (default: current)")
    c.add_argument("--write", action="store_true", help="prepend it to CHANGELOG.md")

    def add_build(p: argparse.ArgumentParser) -> None:
        p.add_argument("--out", help="an empty or new folder (default: dist/release-<ver>)")
        p.add_argument("--tag", help="refuse unless this tag is v<version> (CI passes it)")
        p.add_argument("--allow-dirty", action="store_true", help="not a release: test builds")
        p.add_argument("--no-git-deps", action="store_true", help="skip building sdlc-harness")

    def add_dir(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dir", help="the release folder (default: dist/release-<version>)")

    def add_key(p: argparse.ArgumentParser) -> None:
        p.add_argument("--key", help=f"OpenSSH Ed25519 private key file (or {ENV['key']})")

    def add_trust(p: argparse.ArgumentParser) -> None:
        p.add_argument("--allowed-signers", help=f"or {ENV['allowed_signers']}")
        p.add_argument("--identity", help=f"the signer's principal (or {ENV['identity']})")

    def add_publish(p: argparse.ArgumentParser) -> None:
        p.add_argument("--url", help=f"the internal index's upload URL (or {ENV['url']})")
        p.add_argument("--check-url", help=f"its simple URL (or {ENV['check_url']})")

    def add_smoke(p: argparse.ArgumentParser) -> None:
        p.add_argument("--index-url", help="the simple URL to install from (or --check-url)")
        p.add_argument("--version", help="the version to install (default: current)")
        p.add_argument(
            "--deps-from-lock",
            action="store_true",
            help="third-party packages from uv.lock (offline), not from the index",
        )

    b = sub.add_parser("build", help="wheels, sdists, SBOM, notices, constraints, manifest")
    add_build(b)
    s = sub.add_parser("sign", help="SHA256SUMS and a .sig for every release file")
    add_dir(s)
    add_key(s)
    ve = sub.add_parser("verify", help="check every file, hash and signature")
    add_dir(ve)
    add_trust(ve)
    p = sub.add_parser("publish", help="upload the verified wheels and sdists")
    add_dir(p)
    add_trust(p)
    add_publish(p)
    sm = sub.add_parser("smoke", help="install from the index into a clean venv and dry-run")
    add_smoke(sm)
    r = sub.add_parser("release", help="build, sign, verify, publish, smoke")
    add_build(r)
    add_key(r)
    add_trust(r)
    add_publish(r)
    add_smoke(r)
    r.add_argument("--no-publish", action="store_true", help="stop after verify")
    return parser


COMMANDS = {
    "version": cmd_version,
    "changelog": cmd_changelog,
    "build": cmd_build,
    "sign": cmd_sign,
    "verify": cmd_verify,
    "publish": cmd_publish,
    "smoke": cmd_smoke,
    "release": cmd_release,
}
FAILURES = (
    ReleaseError,
    build.BuildError,
    versions.VersionError,
    signing.SignatureError,
    publish.PublishError,
    smoke.SmokeError,
    OSError,
    RuntimeError,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = Path(args.repo)
    try:
        COMMANDS[args.command](args, repo)
    except FAILURES as error:
        print(f"release {args.command}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if shutil.which("uv") is None:
        print("release: uv is not on PATH (https://docs.astral.sh/uv/)", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
