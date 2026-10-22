"""Several code-intelligence adapters answering as one (M2-43): code-review-graph for the
symbol level, the domain graph across repositories."""

import pytest

from agentic.codeintel import Candidates, CodeIntelError
from agentic.codeintel.combined import Combined


class Intel:
    def __init__(self, name, supports, files=(), note="", fails=False, text=""):
        self.name, self.supports = name, frozenset(supports)
        self._files, self._note, self._fails, self._text = tuple(files), note, fails, text
        self.calls: list[tuple[str, str]] = []

    def _answer(self, op, arg):
        self.calls.append((op, arg))
        if self._fails:
            raise CodeIntelError(f"{self.name} index is broken")
        return Candidates(self._files, source=self.name, note=self._note)

    def dependents(self, path):
        return self._answer("dependents", path)

    def impact(self, diff):
        return self._answer("impact", diff)

    def tests_for(self, path):
        return self._answer("tests_for", path)

    def overview(self):
        self.calls.append(("overview", ""))
        if self._fails:
            raise CodeIntelError(f"{self.name} index is broken")
        return self._text


ALL = {"dependents", "impact", "tests_for", "overview"}


def test_it_is_named_after_its_adapters_and_supports_their_union():
    both = Combined(Intel("symbols", {"dependents", "tests_for"}), Intel("domain", {"impact"}))

    assert both.name == "symbols + domain"
    assert both.supports == {"dependents", "tests_for", "impact"}


def test_files_are_merged_in_adapter_order_without_repeats():
    first = Intel("symbols", ALL, files=["a.py", "b.py"])
    second = Intel("domain", ALL, files=["b.py", "c.py"], note="Services reached: x (1 hop)")

    found = Combined(first, second).dependents("src/m.py")

    assert found.files == ("a.py", "b.py", "c.py")
    assert found.source == "symbols + domain"
    assert found.note == "Services reached: x (1 hop)"
    assert first.calls == second.calls == [("dependents", "src/m.py")]


def test_an_operation_goes_only_to_the_adapters_that_support_it():
    symbols = Intel("symbols", ALL, files=["tests/test_m.py"])
    domain = Intel("domain", {"dependents", "impact"}, files=["other.py"])

    found = Combined(symbols, domain).tests_for("src/m.py")

    assert found.files == ("tests/test_m.py",) and found.source == "symbols"
    assert domain.calls == []


def test_an_adapter_that_answers_nothing_leaves_the_other_answer():
    # DF-85: where code-review-graph answers nothing, the domain graph still does
    found = Combined(Intel("symbols", ALL), Intel("domain", ALL, files=["x.py"])).impact("d")

    assert found.files == ("x.py",) and found.source == "domain"


def test_a_failing_adapter_is_named_and_the_other_still_answers():
    found = Combined(Intel("symbols", ALL, fails=True), Intel("domain", ALL, files=["x.py"]))
    answer = found.dependents("m.py")

    assert answer.files == ("x.py",)
    assert "symbols" in answer.note and "index is broken" in answer.note


def test_every_adapter_failing_raises():
    both = Combined(Intel("a", ALL, fails=True), Intel("b", ALL, fails=True))

    with pytest.raises(CodeIntelError, match="a index is broken"):
        both.dependents("m.py")


def test_overview_joins_each_adapters_overview():
    both = Combined(Intel("symbols", ALL, text="40 files"), Intel("domain", ALL, text="3 svcs"))

    text = both.overview()

    assert "[symbols]\n40 files" in text and "[domain]\n3 svcs" in text


def test_one_adapter_combined_is_that_adapter():
    only = Intel("symbols", ALL)

    assert Combined.of(only) is only
    assert isinstance(Combined.of(only, Intel("domain", ALL)), Combined)
    with pytest.raises(ValueError):
        Combined.of()
