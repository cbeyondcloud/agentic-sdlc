"""Handoff extensions: org, team and repo layers add to the templates (D37, spec §7.4).

A layer's `handoffs/<kind>.yaml` may add fields and sections to a kind, make an
optional field required, or narrow a field's choices. It can never remove,
rename, loosen or retype anything a layer above defined - so a handoff valid for
a lower layer is valid for every layer above it. Org and team layers may also
define new kinds, which start from the base template.

The merged fields become a Pydantic model (`create_model` on the previous one),
so validation, error messages and line numbers work as for the built-in kinds.
"""

import re
import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, TypeAdapter, ValidationError, create_model

from agentic.definitions import DefinitionError
from agentic.handoffs.finalise import FRAMEWORK_FIELDS
from agentic.handoffs.kinds import HandoffBase
from agentic.handoffs.schema import (
    BASE_EXAMPLES,
    BASE_SECTIONS,
    FieldSpec,
    FieldType,
    HandoffCatalog,
    HandoffSchema,
    Layer,
    Section,
)
from agentic.yamlio import Loc, YamlFileError, line_of, load_yaml, read_text

ExtensionLayer = Literal["org", "team", "repo"]
FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
KIND_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
RESERVED = frozenset(FRAMEWORK_FIELDS) | {"status"}
_TOP_KEYS = {"new_kind", "title", "fields", "sections"}
_FIELD_KEYS = {"type", "required", "values", "description", "example"}
_SECTION_KEYS = {"heading", "guidance"}
_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "list": list[str],
}


@dataclass(frozen=True)
class HandoffExtension:
    kind: str
    layer: ExtensionLayer
    path: Path
    new_kind: bool
    title: str | None
    fields: tuple[FieldSpec, ...]
    sections: tuple[Section, ...]
    lines: Mapping[Loc, int] = field(default_factory=dict)  # for error messages

    def where(self, *loc: str | int) -> str:
        line = line_of(dict(self.lines), loc)
        return f" (line {line})" if line else ""


def load_extension(path: Path, layer: ExtensionLayer) -> HandoffExtension:
    """Read one extension file; its kind is the file name. Only the shape is checked
    here - what it may change is checked against the catalog in `extend_catalog`."""
    try:
        data, lines = load_yaml(read_text(path))
    except YamlFileError as error:
        raise DefinitionError(path, [str(error)]) from error
    data = {} if data is None else data
    problems: list[str] = []

    def at(*loc: str | int) -> str:
        line = line_of(lines, loc)
        return f" (line {line})" if line else ""

    if not isinstance(data, dict):
        raise DefinitionError(path, ["an extension file holds key: value fields"])
    kind = path.stem
    if not KIND_NAME.fullmatch(kind):
        problems.append(f"{kind}: a kind name is lowercase letters, digits and dashes")
    for key in sorted(set(data) - _TOP_KEYS):
        problems.append(f"{key}: unexpected key (allowed: {sorted(_TOP_KEYS)}){at(key)}")

    new_kind = data.get("new_kind", False)
    if not isinstance(new_kind, bool):
        problems.append(f"new_kind: must be true or false{at('new_kind')}")
        new_kind = False
    title = data.get("title")
    if new_kind and not (isinstance(title, str) and title.strip()):
        problems.append(f"title: a new kind needs a title{at('title')}")
    if not new_kind and "title" in data:
        problems.append(f"title: only a new kind has a title{at('title')}")

    specs = [_field_spec(name, raw, layer, at, problems) for name, raw in _items(data, "fields")]
    sections = [_section(i, raw, layer, at, problems) for i, raw in _entries(data, "sections")]
    if not isinstance(data.get("fields", {}), dict):
        problems.append(f"fields: must map field names to their settings{at('fields')}")
    if not isinstance(data.get("sections", []), list):
        problems.append(f"sections: must be a list of heading and guidance{at('sections')}")
    if problems:
        raise DefinitionError(path, problems)
    return HandoffExtension(
        kind=kind,
        layer=layer,
        path=path,
        new_kind=new_kind,
        title=title.strip() if new_kind else None,
        fields=tuple(s for s in specs if s is not None),
        sections=tuple(s for s in sections if s is not None),
        lines=lines,
    )


def extend_catalog(
    catalog: HandoffCatalog, extensions: Sequence[HandoffExtension]
) -> HandoffCatalog:
    """The catalog with `extensions` applied in order (org, then team, then repo)."""
    schemas = dict(catalog.schemas)
    for extension in extensions:
        problems: list[str] = []
        if extension.new_kind:
            if extension.layer == "repo":
                problems.append(
                    f"{extension.kind}: new kinds are defined in the policy repo "
                    f"(org or team), not in a repo{extension.where('new_kind')}"
                )
            elif extension.kind in schemas:
                problems.append(
                    f"{extension.kind}: a kind with this name already exists; extend it "
                    f"instead{extension.where('new_kind')}"
                )
            else:
                schemas[extension.kind] = _apply(_new_kind(extension), extension, problems)
        elif extension.kind not in schemas:
            known = ", ".join(sorted(schemas))
            problems.append(f"{extension.kind}: unknown kind (known: {known})")
        else:
            schemas[extension.kind] = _apply(schemas[extension.kind], extension, problems)
        if problems:
            raise DefinitionError(extension.path, problems)
    return HandoffCatalog(schemas)


# ── merging one extension onto a schema ──────────────────────────────────────


def _new_kind(extension: HandoffExtension) -> HandoffSchema:
    return HandoffSchema(
        kind=extension.kind,
        title=extension.title or extension.kind,
        model=create_model(_class_name(extension.kind, "base"), __base__=HandoffBase),
        sections=BASE_SECTIONS,
        examples=dict(BASE_EXAMPLES),
        schema_version=1,
    )


def _apply(schema: HandoffSchema, ext: HandoffExtension, problems: list[str]) -> HandoffSchema:
    model_fields = schema.model.model_fields
    definitions: dict[str, Any] = {}
    examples = {k: v for k, v in schema.examples.items() if k != "open_questions"}
    for spec in ext.fields:
        where = ext.where("fields", spec.name)
        if spec.name in RESERVED:
            problems.append(f"{spec.name}: is a framework field and cannot be defined{where}")
        elif spec.name in model_fields:
            changed = _tighten(spec, model_fields[spec.name], where, problems)
            if changed is not None:
                definitions[spec.name] = changed
                old = examples.get(spec.name)
                if spec.values is not None and old not in spec.values:
                    examples[spec.name] = spec.values[0]  # the template stays valid
        else:
            added = _add(spec, where, problems)
            if added is not None:
                definitions[spec.name] = added
                examples[spec.name] = spec.example

    sections = list(schema.sections)
    for index, section in enumerate(ext.sections):
        if clash := next((s for s in sections if s.heading == section.heading), None):
            problems.append(
                f"sections: '{section.heading}': section already defined by {clash.layer}"
                f"{ext.where('sections', index)}"
            )
        else:
            sections.insert(len(sections) - 1, section)  # before base "Open questions"

    if problems:
        return schema
    model = schema.model
    if definitions:
        model = create_model(
            _class_name(schema.kind, ext.layer), __base__=schema.model, **definitions
        )
    return HandoffSchema(
        kind=schema.kind,
        title=schema.title,
        model=model,
        sections=tuple(sections),
        examples={**examples, "open_questions": schema.examples.get("open_questions", [])},
        schema_version=schema.schema_version,
    )


def _tighten(spec: FieldSpec, current, where: str, problems: list[str]):
    """The new definition of an existing field, or None when nothing changes."""
    annotation = current.annotation
    optional = not current.is_required()
    inner = _without_none(annotation)
    if spec.description or spec.example is not None:
        problems.append(
            f"{spec.name}: an existing field may only change 'required' and 'values'{where}"
        )
        return None
    if spec.type is not None and spec.type != _type_of(inner):
        problems.append(f"{spec.name}: cannot change type to {spec.type}{where}")
        return None
    if spec.required is False and not optional:
        problems.append(f"{spec.name}: cannot loosen a required field to optional{where}")
        return None
    if spec.values is not None:
        choices = typing.get_args(inner) if typing.get_origin(inner) is Literal else None
        if choices is None:
            problems.append(f"{spec.name}: has no choices to narrow{where}")
            return None
        if extra := [v for v in spec.values if v not in choices]:
            problems.append(
                f"{spec.name}: cannot add choices {extra} (allowed: {list(choices)}){where}"
            )
            return None
        inner = Literal[tuple(spec.values)]  # type: ignore[valid-type]
    becomes_required = optional and spec.required is True
    if spec.values is None and not becomes_required:
        return None
    # Keep the field's constraints (ge, le, min_length, ...): Pydantic holds them in
    # the FieldInfo's metadata, not in the annotation, so they travel with it here.
    kept = Annotated[inner, *current.metadata] if current.metadata else inner
    if optional and not becomes_required:
        return (kept | None, Field(default=current.default, description=current.description))
    return (kept, Field(description=current.description))


def _add(spec: FieldSpec, where: str, problems: list[str]):
    """The definition of a new field, or None after recording why it is refused."""
    missing = [k for k in ("type", "description", "example") if not _given(spec, k)]
    if missing:
        problems.append(f"{spec.name}: a new field needs {', '.join(missing)}{where}")
        return None
    if spec.values is not None and spec.type != "enum":
        problems.append(f"{spec.name}: values apply only to an enum (type: enum){where}")
        return None
    if spec.type == "enum":
        if not spec.values:
            problems.append(f"{spec.name}: an enum needs its values{where}")
            return None
        annotation: Any = Literal[tuple(spec.values)]  # type: ignore[valid-type]
    else:
        annotation = _TYPES[spec.type]  # type: ignore[index]
    try:
        TypeAdapter(annotation).validate_python(spec.example, strict=True)
    except ValidationError:
        problems.append(
            f"{spec.name}: the example {spec.example!r} is not a valid {spec.type}{where}"
        )
        return None
    if spec.required:
        if spec.type == "string":  # a blank value says nothing (DF-33); no schema change
            return (Annotated[str, AfterValidator(_not_blank)], ...)
        return (annotation, ...)
    return (annotation, []) if spec.type == "list" else (annotation | None, None)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("a required value must not be blank")
    return value


# ── reading one file ─────────────────────────────────────────────────────────


def _field_spec(name, raw, layer, at, problems) -> FieldSpec | None:
    if not isinstance(name, str) or not FIELD_NAME.fullmatch(name):
        problems.append(
            f"{name}: a field name is lowercase letters, digits and underscores, "
            f"starting with a letter{at('fields', name)}"
        )
        return None
    if name.startswith("model_") or hasattr(BaseModel, name):  # json, copy, model_dump (DF-31)
        problems.append(
            f"{name}: clashes with a name the handoff model uses; choose another"
            f"{at('fields', name)}"
        )
        return None
    if not isinstance(raw, dict):
        problems.append(f"{name}: must map settings such as type and required{at('fields', name)}")
        return None
    for key in sorted(set(raw) - _FIELD_KEYS):
        allowed = sorted(_FIELD_KEYS)
        problems.append(
            f"{name}.{key}: unexpected key (allowed: {allowed}){at('fields', name, key)}"
        )
    kind = raw.get("type")
    if kind is not None and kind not in typing.get_args(FieldType):
        problems.append(
            f"{name}.type: {kind!r} is not one of {list(typing.get_args(FieldType))}"
            f"{at('fields', name, 'type')}"
        )
    required = raw.get("required")
    if required is not None and not isinstance(required, bool):
        problems.append(f"{name}.required: must be true or false{at('fields', name, 'required')}")
    values = raw.get("values")
    if values is not None and not (
        isinstance(values, list) and values and all(isinstance(v, str) for v in values)
    ):
        problems.append(f"{name}.values: must be a list of words{at('fields', name, 'values')}")
        values = None
    description = raw.get("description")
    if description is not None and not (isinstance(description, str) and description.strip()):
        problems.append(f"{name}.description: must be text{at('fields', name, 'description')}")
    return FieldSpec(
        name=name,
        type=kind,
        required=required if isinstance(required, bool) else None,
        values=tuple(values) if values else None,
        description=description or "",
        example=raw.get("example"),
        layer=layer,
    )


def _section(index, raw, layer, at, problems) -> Section | None:
    where = at("sections", index)
    if not isinstance(raw, dict):
        problems.append(f"sections[{index}]: must have a heading and guidance{where}")
        return None
    for key in sorted(set(raw) - _SECTION_KEYS):
        problems.append(f"sections[{index}].{key}: unexpected key{where}")
    heading, guidance = raw.get("heading"), raw.get("guidance")
    # one line: a body line can never match a heading with a line break in it (DF-32)
    if not (
        isinstance(heading, str)
        and 0 < len(heading.strip()) <= 80
        and "#" not in heading
        and len(heading.strip().splitlines()) == 1
    ):
        problems.append(f"sections[{index}].heading: 1-80 characters on one line, no '#'{where}")
        return None
    if not (isinstance(guidance, str) and guidance.strip()):
        problems.append(f"sections[{index}].guidance: say what the agent writes there{where}")
        return None
    if "-->" in guidance or "<!--" in guidance:  # it is rendered inside <!-- ... --> (DF-32)
        problems.append(f"sections[{index}].guidance: may not contain '<!--' or '-->'{where}")
        return None
    return Section(heading.strip(), guidance.strip(), layer)


def _items(data: dict, key: str) -> list[tuple]:
    value = data.get(key)
    return list(value.items()) if isinstance(value, dict) else []


def _entries(data: dict, key: str) -> list[tuple]:
    value = data.get(key)
    return list(enumerate(value)) if isinstance(value, list) else []


def _given(spec: FieldSpec, key: str) -> bool:
    if key == "type":
        return spec.type is not None
    if key == "description":
        return bool(spec.description)
    return spec.example is not None


def _without_none(annotation):
    """`X | None` -> X; anything else unchanged."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        rest = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(rest) == 1:
            return rest[0]
    return annotation


def _type_of(annotation) -> FieldType | None:
    if typing.get_origin(annotation) is Literal:
        return "enum"
    for name, python in _TYPES.items():
        if annotation == python:
            return name  # type: ignore[return-value]
    return None


def _class_name(kind: str, layer: Layer) -> str:
    return f"{kind.replace('-', '_')}_{layer}"


__all__ = ["ExtensionLayer", "HandoffExtension", "extend_catalog", "load_extension"]
