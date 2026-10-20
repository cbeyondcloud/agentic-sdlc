"""Helpers for the skills tests: build skill folders and catalogs in a temporary folder."""

from pathlib import Path

from agentic.yamlio import dump_yaml

MIT = "MIT License\n\nCopyright (c) 2026 Example\n\nPermission is hereby granted...\n"


def skill_md(name: str, description: str, body: str = "Do the thing well.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n"


def make_skill(
    root: Path,
    key: str,
    *,
    description: str | None = None,
    body: str = "Do the thing well.",
    files: dict[str, str | bytes] | None = None,
    licence_file: bool = True,
) -> Path:
    """A skill folder `<root>/<source>/<name>/` with a SKILL.md, a licence and extra files."""
    name = key.split("/")[1]
    folder = root / key
    folder.mkdir(parents=True, exist_ok=True)
    text = skill_md(name, description or f"Use when you need {name}.", body)
    (folder / "SKILL.md").write_text(text, encoding="utf-8", newline="\n")
    if licence_file:
        (folder / "LICENSE.txt").write_text(MIT, encoding="utf-8", newline="\n")
    for relative, content in (files or {}).items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
    return folder


def entry(digest: str, **overrides) -> dict:
    return {
        "source": "https://github.com/example/skills",
        "commit": "a" * 40,
        "path": "skills/x",
        "licence": "MIT",
        "owner": "platform-team",
        "version": "1.0.0",
        "sha256": digest,
        **overrides,
    }


def write_catalog(root: Path, skills: dict[str, dict], **extra) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "catalog.yaml"
    path.write_text(dump_yaml({"skills": skills, **extra}), encoding="utf-8", newline="\n")
    return path
