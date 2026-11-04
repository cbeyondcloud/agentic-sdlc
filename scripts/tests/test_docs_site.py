"""The docs site (M3-23): `mkdocs build --strict` over docs/ fails on a broken link or
anchor, every page of the site is in the navigation, and the built site loads nothing
from another host (no analytics, font host or CDN: orgs host it internally, offline).

Needs the optional docs group (`uv sync --group docs`); skipped, with that hint, without
it. CI installs the group."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "mkdocs.yml"

mkdocs = pytest.importorskip(
    "mkdocs", reason="the docs group is not installed: run `uv sync --group docs`"
)
yaml = pytest.importorskip("yaml")

#: a resource the browser fetches by itself: scripts, stylesheets, images, fonts, frames
REMOTE_LOAD = re.compile(
    r"""<(?:script|img|iframe|source)\b[^>]*\bsrc=["']?(?:https?:)?//"""
    r"""|<link\b[^>]*\bhref=["']?(?:https?:)?//"""
    r"""|@import\s+(?:url\()?["']?(?:https?:)?//"""
    r"""|url\(\s*["']?(?:https?:)?//""",
    re.IGNORECASE,
)


def build(config: Path, site: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "-f", str(config), "-d", str(site)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=config.parent,
    )


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, str]:
    site = tmp_path_factory.mktemp("site")
    done = build(CONFIG, site)
    assert done.returncode == 0, done.stderr[-4000:]
    return site, done.stderr


def test_the_strict_build_passes(built):
    site, _ = built
    assert (site / "index.html").is_file()
    assert (site / "guides" / "developer.html").is_file()
    assert (site / "training" / "deck.html").is_file()


def test_every_page_is_in_the_navigation(built):
    _, log = built
    assert 'not included in the "nav"' not in log, log


def test_the_site_loads_nothing_from_another_host(built):
    site, _ = built
    offenders = []
    for path in sorted(site.rglob("*")):
        if path.suffix in (".html", ".css", ".js") and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in REMOTE_LOAD.finditer(text):
                offenders.append(f"{path.relative_to(site).as_posix()}: {match.group(0)}")
    assert not offenders, offenders


def test_the_config_turns_no_analytics_on():
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    theme = config["theme"]
    assert "analytics" not in theme and "google_analytics" not in config
    assert theme.get("highlightjs") is False  # the theme loads highlight.js from a CDN


def test_a_broken_link_fails_the_build(tmp_path: Path):
    """The same validation settings, on a page with a link to a missing page."""
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for key in ("nav", "exclude_docs"):
        config.pop(key, None)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# Home\n\nSee [the guide](missing.md).\n", "utf-8")
    (tmp_path / "mkdocs.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    done = build(tmp_path / "mkdocs.yml", tmp_path / "site")
    assert done.returncode != 0
    assert "missing.md" in done.stderr


def test_a_broken_anchor_fails_the_build(tmp_path: Path):
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for key in ("nav", "exclude_docs"):
        config.pop(key, None)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# Home\n\nSee [steps](other.md#no-such-heading).\n", "utf-8")
    (docs / "other.md").write_text("# Other\n\n## Steps\n", "utf-8")
    (tmp_path / "mkdocs.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    done = build(tmp_path / "mkdocs.yml", tmp_path / "site")
    assert done.returncode != 0
    assert "no-such-heading" in done.stderr


def test_the_site_is_never_committed():
    assert shutil.which("git") is not None
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "site/index.html"], cwd=ROOT, capture_output=True
    )
    assert ignored.returncode == 0, "add site/ to .gitignore"
