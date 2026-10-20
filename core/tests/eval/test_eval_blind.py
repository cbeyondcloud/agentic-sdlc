"""Blinding (M1-63, spike S10): scrubber, X/Y labels sealed until scoring, leak check."""

import pytest

from agentic.eval.blind import BlindingError, LeakCheck, Scrubber, assign_labels


def test_scrubber_removes_tells_names_and_self_identification():
    text = (
        "Using skill: acceptance-criteria-writer\n"
        "As the business analyst agent, I wrote the requirements below.\n"
        "I am an AI language model trained by Somevendor.\n"
        "## Requirements\n"
        "REQ-1 exports invoices; spec-driven-development helps here. MARKER-42\n"
    )
    scrubbed = Scrubber(
        names=["spec-driven-development", "acceptance-criteria-writer"], markers=["MARKER-42"]
    ).scrub(text)
    assert "Using skill" not in scrubbed
    assert "As the business analyst agent" not in scrubbed
    assert "language model" not in scrubbed
    assert "spec-driven-development" not in scrubbed.lower()
    assert "MARKER-42" not in scrubbed
    assert "## Requirements" in scrubbed and "REQ-1 exports invoices" in scrubbed


def test_scrubber_leaves_ordinary_text_alone():
    text = "## Design\nWe use a skilled queue worker; the agent pool is unchanged.\n"
    assert Scrubber(names=["tdd"]).scrub(text) == text


def test_names_are_matched_case_insensitively_on_word_boundaries():
    scrubber = Scrubber(names=["writing-plans"])
    assert "writing-plans" not in scrubber.scrub("Per Writing-Plans, do X.").lower()
    assert scrubber.scrub("rewriting-plansville") == "rewriting-plansville"


def test_short_terms_are_refused_because_they_match_everything():
    with pytest.raises(BlindingError, match="at least 3"):
        LeakCheck(["qa"])
    with pytest.raises(BlindingError, match="at least 3"):
        Scrubber(names=["x"])


def test_labels_flip_with_the_seed_and_stay_sealed_until_scored():
    seen = set()
    for seed in range(20):
        mapping = assign_labels(["baseline", "with-skill"], seed=seed)
        seen.add(mapping.label("baseline"))
        assert {mapping.label("baseline"), mapping.label("with-skill")} == {"X", "Y"}
        with pytest.raises(BlindingError, match="sealed"):
            mapping.reveal()
        mapping.seal_scores()
        assert set(mapping.reveal().values()) == {"baseline", "with-skill"}
    assert seen == {"X", "Y"}  # both orders occur
    assert assign_labels(["a1", "b1"], seed=5).label("a1") == assign_labels(
        ["a1", "b1"], seed=5
    ).label("a1")


def test_a_single_variant_is_labelled_x():
    mapping = assign_labels(["only"], seed=3)
    assert mapping.label("only") == "X"


def test_leak_check_finds_unscrubbed_terms():
    check = LeakCheck(["with-skill", "acme-model-7"])
    check.see("Response 1: fine.\nResponse 2: fine.")
    assert check.leaks == []
    check.see("this answer was produced With-Skill enabled")
    assert check.leaks == ["with-skill"]
    assert check.inputs_seen == 2
