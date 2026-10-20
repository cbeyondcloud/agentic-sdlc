"""Loading an eval corpus (M1-59, M1-63; spike S10 corpus rules).

A corpus is a folder:

    cases/<class>/<id>.yaml     one EvalCase per file
    golden/, workspaces/, ...   files the cases point at (corpus-relative paths)
    rubrics/<id>.yaml           the rubrics its scoping cases name
    manifest.json               sha256 of every file above, and of the whole set

Corpora live where their data belongs (decision 17): `library/eval/` holds only
synthetic or permissively licensed public cases (`load_corpus(public=True)` enforces
it); pilot-repo cases live in the policy repo's `eval/` or a repo's `.agentic/eval/`.

The manifest pins what a report or A/B plan was measured on. Hashes are over bytes
with CRLF turned into LF, so a Windows checkout and a Linux checkout agree. Recorded
responses (`cassettes/`), expected reports (`expected/`) and nested corpora
(`fixtures/`) are not part of the manifest: re-recording does not change the cases.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, ValidationError

from agentic.definitions import DefinitionError
from agentic.eval.cases import CaseClass, EvalCase, Split
from agentic.yamlio import YamlFileError, load_yaml, read_text

MANIFEST = "manifest.json"
NOT_IN_MANIFEST = frozenset({MANIFEST, "cassettes", "expected", "fixtures"})
#: Licences a public corpus case may carry (the repo's allow-list, OSS-first rule).
PERMISSIVE = frozenset(
    {"MIT", "MIT-0", "APACHE-2.0", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "0BSD", "ISC"}
    | {"POSTGRESQL", "UNLICENSE", "CC0-1.0"}
)
#: Decision 18 (spike S10): the holdout cases, units of truth and dev cases each class
#: needs before the M1-64 gate can tell a pass from a fail.
DECISION_18_FLOORS: dict[str, dict[str, int]] = {
    "scoping": {"holdout": 20, "dev": 5},
    "review": {"holdout": 20, "truth": 40, "dev": 5},
    "dev_task": {"holdout": 20, "dev": 5},
    "qa": {"truth": 30, "dev": 5},
}
TRUTH_NAMES = {
    "review": "seeded defects",
    "qa": "seeded failing criteria",
    "dev_task": "fail-to-pass tests",
    "defect": "fail-to-pass tests",
    "scoping": "units of truth",
}


class CorpusError(DefinitionError):
    """The corpus could not be loaded or is inconsistent."""

    kind = "eval corpus"


class CorpusManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    files: dict[str, str]  # POSIX path -> sha256:<hex> of its LF-normalised bytes
    sha: str  # sha256 over the sorted (path, digest) pairs


@dataclass(frozen=True)
class Corpus:
    root: Path
    cases: list[EvalCase]
    manifest: CorpusManifest
    _by_id: dict[str, EvalCase] = field(default_factory=dict, repr=False)

    def select(self, agent: str | None = None, split: Split | None = None) -> list[EvalCase]:
        """The cases for one agent and/or split, in id order."""
        return [
            case
            for case in self.cases
            if (agent is None or case.agent == agent) and (split is None or case.split == split)
        ]

    def case(self, case_id: str) -> EvalCase:
        return self._by_id[case_id]

    def path(self, relative: str) -> Path:
        """A file a case points at (already checked to be inside the corpus)."""
        return self.root / PurePosixPath(relative)

    def counts(self) -> dict[str, dict[str, int]]:
        """Per class: cases, holdout, dev, and units of truth in the holdout split."""
        out: dict[str, dict[str, int]] = {}
        for case in self.cases:
            row = out.setdefault(case.case_class, {"cases": 0, "holdout": 0, "dev": 0, "truth": 0})
            row["cases"] += 1
            row[case.split] += 1
            if case.split == "holdout":
                row["truth"] += case.truth_count
        return out


def load_corpus(root: Path, *, public: bool = False) -> Corpus:
    """Every case under `root/cases/`, validated, with the manifest checked when present.
    Raises CorpusError listing every problem."""
    root = Path(root)
    problems: list[str] = []
    cases: dict[str, EvalCase] = {}
    # a case saved as .yml (or anything else) must be an error, never silently skipped
    stray = (p for p in (root / "cases").rglob("*") if p.is_file() and p.suffix != ".yaml")
    for path in sorted(p for p in stray if not any(_scratch(part) for part in p.parts)):
        problems.append(f"{path.relative_to(root).as_posix()}: a case file ends in .yaml")
    for path in sorted((root / "cases").rglob("*.yaml")):
        where = path.relative_to(root).as_posix()
        try:
            data, lines = load_yaml(read_text(path))
            case = EvalCase.model_validate(data)
        except YamlFileError as error:
            problems.append(f"{where}: {error}")
            continue
        except ValidationError as error:
            problems += [
                f"{where}: {line}"
                for line in DefinitionError.from_validation_error(
                    path, error, "case", lines=lines
                ).errors
            ]
            continue
        if case.id in cases:
            problems.append(f"{where}: duplicate case id '{case.id}'")
            continue
        reference_problems = _reference_problems(root, case)
        problems += [f"{where}: {p}" for p in reference_problems]
        if not reference_problems and case.inputs.handoff_files:
            if both := sorted(case.inputs.handoffs.keys() & case.inputs.handoff_files.keys()):
                problems.append(f"{where}: handoff {both[0]} given both inline and as a file")
            else:
                case = _with_handoff_files(root, case)
        if public and not _public(case):
            problems.append(
                f"{where}: a public corpus holds only synthetic or permissively licensed "
                f"cases, each under an allow-listed licence (decision 17); "
                f"'{case.source.licence}' from {case.source.repo} belongs in the policy "
                f"repo's eval/ folder"
            )
        cases[case.id] = case
    manifest = build_manifest(root)
    recorded = root / MANIFEST
    if recorded.is_file():
        try:
            pinned = CorpusManifest.model_validate_json(recorded.read_bytes())
        except ValidationError:
            problems.append(f"{MANIFEST}: not a valid manifest; rewrite it")
        else:
            if pinned.sha != manifest.sha:
                changed = sorted(
                    p
                    for p in pinned.files.keys() | manifest.files.keys()
                    if pinned.files.get(p) != manifest.files.get(p)
                )
                problems.append(
                    f"{MANIFEST} is out of date ({', '.join(changed[:5])}); review the change "
                    "and rewrite it with write_manifest"
                )
    if problems:
        raise CorpusError(root, problems)
    ordered = [cases[k] for k in sorted(cases)]
    return Corpus(root=root, cases=ordered, manifest=manifest, _by_id=dict(cases))


def build_manifest(root: Path) -> CorpusManifest:
    root = Path(root)
    files = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        if relative.parts[0] in NOT_IN_MANIFEST or any(_scratch(p) for p in relative.parts):
            continue
        files[relative.as_posix()] = file_digest(path)
    return CorpusManifest(files=files, sha=_sha(files))


def write_manifest(root: Path) -> CorpusManifest:
    manifest = build_manifest(root)
    text = json.dumps(manifest.model_dump(), indent=2, sort_keys=True) + "\n"
    (Path(root) / MANIFEST).write_bytes(text.encode("utf-8"))
    return manifest


def file_digest(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def floor_shortfalls(
    corpus: Corpus, floors: Mapping[str, Mapping[str, int]] = DECISION_18_FLOORS
) -> list[str]:
    """What the corpus lacks against `floors`, one line per class and measure."""
    counts = corpus.counts()
    out = []
    for case_class, needs in floors.items():
        have = counts.get(case_class, {"cases": 0, "holdout": 0, "dev": 0, "truth": 0})
        for measure, need in needs.items():
            if have[measure] >= need:
                continue
            what = {
                "holdout": "holdout cases",
                "dev": "dev cases",
                "truth": f"{TRUTH_NAMES[case_class]} in holdout",
            }[measure]
            out.append(f"{case_class}: {have[measure]} {what}, needs {need}")
    return out


def rubric_path(rubric_id: str) -> str:
    """The corpus-relative path of a rubric (its id is a slug, checked by EvalCase)."""
    return f"rubrics/{rubric_id}.yaml"


def _reference_problems(root: Path, case: EvalCase) -> list[str]:
    problems = []
    resolved_root = root.resolve()
    references = case.references()
    if case.rubric:  # read by the judge and copied next to the report: inside, and there
        references.append(rubric_path(case.rubric))
    for reference in references:
        pure = PurePosixPath(reference)
        target = (root / pure).resolve()
        if pure.is_absolute() or ".." in pure.parts or "\\" in reference:
            problems.append(f"'{reference}' must be a relative path inside the corpus")
        elif not target.is_relative_to(resolved_root):
            problems.append(f"'{reference}' must be a relative path inside the corpus")
        elif not target.exists():
            problems.append(f"'{reference}' does not exist in the corpus")
    return problems


def _with_handoff_files(root: Path, case: EvalCase) -> EvalCase:
    """The case with its handoff files read (LF) into `inputs.handoffs`."""
    read = {
        kind: read_text(root / PurePosixPath(path)).replace("\r\n", "\n")
        for kind, path in case.inputs.handoff_files.items()
    }
    inputs = case.inputs.model_copy(update={"handoffs": {**case.inputs.handoffs, **read}})
    return case.model_copy(update={"inputs": inputs})


def _scratch(part: str) -> bool:
    """Caches and hidden files (running a workspace's tests leaves __pycache__ behind)."""
    return part == "__pycache__" or part.startswith(".")


def _public(case: EvalCase) -> bool:
    """Synthetic or not, the case's content must carry an allow-listed licence: a
    `synthetic: true` flag on content copied from a copyleft repo must not pass."""
    return case.source.licence.upper() in PERMISSIVE


def _sha(files: Mapping[str, str]) -> str:
    canonical = "\n".join(f"{path}\0{digest}" for path, digest in sorted(files.items()))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "DECISION_18_FLOORS",
    "MANIFEST",
    "CaseClass",
    "Corpus",
    "CorpusError",
    "CorpusManifest",
    "build_manifest",
    "file_digest",
    "floor_shortfalls",
    "load_corpus",
    "rubric_path",
    "write_manifest",
]
