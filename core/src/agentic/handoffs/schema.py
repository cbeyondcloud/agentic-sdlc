"""What a handoff of each kind must contain: fields and body sections (D37, spec §7.4).

A schema is built from layers, top to bottom, each only adding or tightening:
the base (`HandoffBase` and the Summary / Open questions sections), the kind
(the five built-ins), then org, team and repo extensions (`extensions.py`).

The built-in sections and example values are read from `templates/*.md`. Each
file holds what its layer introduces: front matter with example values for its
fields, then a `# <title>` line and its `## <heading>` sections, each with one
`<!-- guidance -->` comment. `base.md`'s two sections go first and last.

Template sources (THIRD_PARTY_NOTICES.md): requirements adapts the github/spec-kit
spec template; design the spec-kit plan template and MADR; review follows the
structure of Zyroflo's P6 stage and review rubric; the rest are our own.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel

from agentic.handoffs.kinds import HANDOFF_KINDS, HandoffBase
from agentic.yamlio import dump_yaml, load_yaml, split_front_matter

Layer = Literal["base", "kind", "org", "team", "repo"]
FieldType = Literal["string", "integer", "number", "boolean", "list", "enum"]
_GUIDANCE = re.compile(r"<!--(.*?)-->", re.DOTALL)


@dataclass(frozen=True)
class Section:
    heading: str  # the text after '## '
    guidance: str  # what the agent writes there
    layer: Layer


@dataclass(frozen=True)
class FieldSpec:
    """A field added (or tightened) by an extension; built-in fields are typed by their model."""

    name: str
    type: FieldType | None
    required: bool | None  # None: not stated (an existing field keeps its own)
    values: tuple[str, ...] | None
    description: str
    example: object
    layer: Layer


@dataclass(frozen=True)
class HandoffSchema:
    kind: str
    title: str  # the document's H1, e.g. "Design"
    model: type[BaseModel]  # the merged fields (a HandoffBase subclass)
    sections: tuple[Section, ...]  # in document order; every one is required
    examples: Mapping[str, object]  # an example value per field, for the template
    schema_version: int

    @property
    def digest(self) -> str:
        """Identifies exactly what this schema requires (fields and sections)."""
        content = {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "fields": self.model.model_json_schema(),
            "sections": [[s.heading, s.guidance] for s in self.sections],
        }
        return _sha256(content)


class HandoffCatalog:
    """Every handoff kind visible to a run, with its merged schema."""

    def __init__(self, schemas: Mapping[str, HandoffSchema]) -> None:
        self.schemas: dict[str, HandoffSchema] = dict(schemas)

    def schema(self, kind: str) -> HandoffSchema:
        if kind not in self.schemas:
            known = ", ".join(sorted(self.schemas))
            raise ValueError(f"unknown handoff kind {kind!r} (known: {known})")
        return self.schemas[kind]

    def kinds(self) -> list[str]:
        return sorted(self.schemas)

    @property
    def digest(self) -> str:
        return _sha256({kind: self.schemas[kind].digest for kind in sorted(self.schemas)})


def render_template(schema: HandoffSchema) -> str:
    """The document an agent is shown: example front matter, then every section
    heading with its guidance."""
    header = dump_yaml({"status": "complete", **schema.examples})
    parts = [f"---\n{header}---\n", f"# {schema.title}: <short title>\n"]
    for section in schema.sections:
        parts.append(f"## {section.heading}\n<!-- {section.guidance} -->\n")
    return "\n".join(parts)


def _template_file(name: str) -> tuple[dict, str, tuple[Section, ...]]:
    """(example values, title, sections) of one built-in template file."""
    text = (files("agentic.handoffs") / "templates" / f"{name}.md").read_text(encoding="utf-8")
    header, body = split_front_matter(text)
    examples, _ = load_yaml(header)
    lines = body.split("\n")
    title = lines[0].removeprefix("# ").strip()
    layer: Layer = "base" if name == "base" else "kind"
    sections = []
    for chunk in re.split(r"^## ", "\n".join(lines[1:]), flags=re.MULTILINE)[1:]:
        heading, _, rest = chunk.partition("\n")
        guidance = _GUIDANCE.search(rest)
        sections.append(
            Section(heading.strip(), guidance.group(1).strip() if guidance else "", layer)
        )
    return examples or {}, title, tuple(sections)


def _builtin_catalog() -> HandoffCatalog:
    summary, open_questions = BASE_SECTIONS
    schemas = {}
    for kind, model in HANDOFF_KINDS.items():
        examples, title, sections = _template_file(kind)
        schemas[kind] = HandoffSchema(
            kind=kind,
            title=title,
            model=model,
            sections=(summary, *sections, open_questions),
            examples={
                "summary": BASE_EXAMPLES["summary"],
                **examples,
                "open_questions": BASE_EXAMPLES["open_questions"],
            },
            schema_version=model.SCHEMA_VERSION,  # type: ignore[attr-defined]
        )
    return HandoffCatalog(schemas)


def _sha256(content: object) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_base_examples, _, BASE_SECTIONS = _template_file("base")
BASE_EXAMPLES: Mapping[str, object] = dict(_base_examples)  # summary, open_questions
BUILTIN_CATALOG = _builtin_catalog()

__all__ = [
    "BASE_EXAMPLES",
    "BASE_SECTIONS",
    "BUILTIN_CATALOG",
    "FieldSpec",
    "FieldType",
    "HandoffBase",
    "HandoffCatalog",
    "HandoffSchema",
    "Layer",
    "Section",
    "render_template",
]
