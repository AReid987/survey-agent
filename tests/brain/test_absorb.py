"""Tests for the ask-once escalation protocol (docs/adr/0002).

The behaviour under test is the promise made to the owner: the agent texts
about a given question at most once, ever. Everything here is a check on that
promise holding under the ways surveys actually re-present questions.
"""

from __future__ import annotations

import json

from survey_agent import brain
from survey_agent.brain.normalize import hash_question, normalize_question
from survey_agent.brain.schemas import BrainResponse
from survey_agent.brain.store import FailureStore


def _response(
    question_text: str = "What is your favorite brand of coffee?",
    answer: str = "Community Coffee",
    site: str = "branded-surveys",
    options: list[str] | None = None,
    response_id: str = "8a4b0000-0000-4000-8000-000000000001",
) -> BrainResponse:
    return BrainResponse(
        response_id=response_id,
        question_hash=hash_question(question_text, options),
        question_text=question_text,
        options=list(options or []),
        answer=answer,
        site=site,
        source="owner_text",
        created_at="2026-08-26T10:00:00+00:00",
    )


# ── Question identity ───────────────────────────────────────────────────


def test_normalize_question_collapses_cosmetic_differences():
    base = normalize_question("What is your household income?")
    assert normalize_question("  What is your   household income?  ") == base
    assert normalize_question("WHAT IS YOUR HOUSEHOLD INCOME?") == base
    assert normalize_question("What is your household income?*") == base


def test_normalize_question_strips_page_enumeration():
    """Surveys prefix the same question with a per-page number."""
    base = normalize_question("How many people live in your home?")
    assert normalize_question("12. How many people live in your home?") == base
    assert normalize_question("3) How many people live in your home?") == base


def test_hash_question_ignores_option_order():
    """Option shuffling is an anti-bot tactic; it must not force a re-ask."""
    a = hash_question("Pick one", ["Yes", "No", "Maybe"])
    b = hash_question("Pick one", ["Maybe", "No", "Yes"])
    assert a == b


def test_hash_question_distinguishes_different_option_sets():
    """Same prompt with a genuinely different option set is a new question."""
    a = hash_question("Pick one", ["Yes", "No"])
    b = hash_question("Pick one", ["Yes", "No", "Prefer not to say"])
    assert a != b


def test_hash_question_is_64_hex_chars():
    assert len(hash_question("anything")) == 64
    int(hash_question("anything"), 16)  # raises if not hex


# ── Store round-trip ────────────────────────────────────────────────────


def test_save_response_round_trips_through_sqlite(brain_dir):
    store = FailureStore(brain_dir)
    resp = _response()
    assert store.save_response(resp) is True  # new question

    found = store.find_response(resp.question_hash)
    assert found is not None
    assert found.answer == "Community Coffee"
    assert found.question_text == "What is your favorite brand of coffee?"


def test_save_response_also_appends_audit_jsonl(brain_dir):
    store = FailureStore(brain_dir)
    store.save_response(_response())

    lines = (brain_dir / "responses.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["answer"] == "Community Coffee"


def test_save_response_reports_new_vs_existing(brain_dir):
    store = FailureStore(brain_dir)
    resp = _response()
    assert store.save_response(resp) is True
    assert store.save_response(resp) is False  # same question again


def test_find_response_returns_none_for_unknown_question(brain_dir):
    store = FailureStore(brain_dir)
    assert store.find_response("f" * 64) is None


def test_reabsorbing_corrects_the_answer_but_keeps_recurrence_count(brain_dir):
    """The owner correcting themselves must not erase how often it recurred."""
    store = FailureStore(brain_dir)
    resp = _response(answer="Folgers")
    store.save_response(resp)
    store.mark_response_used(resp.question_hash, "2026-08-26T11:00:00+00:00")
    store.mark_response_used(resp.question_hash, "2026-08-26T12:00:00+00:00")

    corrected = _response(answer="Community Coffee", response_id="8a4b0000-0000-4000-8000-000000000002")
    store.save_response(corrected)

    found = store.find_response(resp.question_hash)
    assert found is not None
    assert found.answer == "Community Coffee"  # corrected
    assert found.hit_count == 2  # history preserved


def test_mark_response_used_increments_hit_count(brain_dir):
    store = FailureStore(brain_dir)
    resp = _response()
    store.save_response(resp)

    store.mark_response_used(resp.question_hash, "2026-08-26T11:00:00+00:00")
    found = store.find_response(resp.question_hash)
    assert found is not None
    assert found.hit_count == 1
    assert found.last_used_at == "2026-08-26T11:00:00+00:00"


# ── Public API: the ask-once promise ────────────────────────────────────


def test_lookup_returns_none_before_the_owner_has_ever_answered(brain_dir):  # noqa: ARG001
    assert brain.lookup("Have you purchased a vehicle in the last 12 months?") is None


def test_absorb_then_lookup_resolves_without_asking_again(brain_dir):  # noqa: ARG001
    question = "Have you purchased a vehicle in the last 12 months?"
    brain.absorb(question, "No", site="branded-surveys")

    found = brain.lookup(question)
    assert found is not None
    assert found.answer == "No"


def test_lookup_hits_across_cosmetic_rewording(brain_dir):  # noqa: ARG001
    """The whole point: a reworded re-render must not trigger a second text."""
    brain.absorb("How many children live in your household?", "3", site="branded-surveys")

    assert brain.lookup("  How many children live in your household?  ") is not None
    assert brain.lookup("7. how many children live in your household?") is not None


def test_lookup_hits_across_sites(brain_dir):  # noqa: ARG001
    """An answer learned on one site resolves the same question elsewhere."""
    brain.absorb("What is your ZIP code?", "78736", site="branded-surveys")

    found = brain.lookup("What is your ZIP code?")
    assert found is not None
    assert found.answer == "78736"


def test_lookup_counts_the_hit_so_recurrence_is_visible(brain_dir):  # noqa: ARG001
    question = "Do you own or rent your home?"
    brain.absorb(question, "Rent", site="branded-surveys")

    brain.lookup(question)
    brain.lookup(question)

    answers = brain.known_answers()
    assert len(answers) == 1
    assert answers[0].hit_count == 2


def test_absorb_normalizes_the_site(brain_dir):  # noqa: ARG001
    resp = brain.absorb("Q?", "A", site="  Branded-Surveys  ")
    assert resp.site == "branded-surveys"


def test_absorb_rejects_empty_question(brain_dir):  # noqa: ARG001
    import pytest

    with pytest.raises(ValueError, match="question must not be empty"):
        brain.absorb("   ", "some answer", site="branded-surveys")


def test_absorb_rejects_empty_answer(brain_dir):  # noqa: ARG001
    import pytest

    with pytest.raises(ValueError, match="answer must not be empty"):
        brain.absorb("A real question?", "  ", site="branded-surveys")


def test_absorb_raises_on_store_failure_rather_than_silently_dropping(brain_dir, monkeypatch):  # noqa: ARG001
    """A silent save failure would re-text the owner — the bug adr/0002 kills."""
    import pytest

    from survey_agent.brain import query as query_mod

    def _boom(self, response):  # noqa: ARG001
        raise RuntimeError("disk full")

    monkeypatch.setattr(FailureStore, "save_response", _boom)
    query_mod._BrainState._store = None

    with pytest.raises(RuntimeError, match="disk full"):
        brain.absorb("Q?", "A", site="branded-surveys")


def test_lookup_returns_none_on_store_failure_so_caller_escalates(brain_dir, monkeypatch):  # noqa: ARG001
    """Read failure must fail toward asking, never toward a wrong answer."""

    def _boom(self, question_hash):  # noqa: ARG001
        raise RuntimeError("db locked")

    monkeypatch.setattr(FailureStore, "find_response", _boom)

    assert brain.lookup("anything") is None


def test_known_answers_returns_newest_first(brain_dir):  # noqa: ARG001
    brain.absorb("First question?", "1", site="branded-surveys")
    brain.absorb("Second question?", "2", site="branded-surveys")

    answers = brain.known_answers()
    assert len(answers) == 2
    assert {a.answer for a in answers} == {"1", "2"}


def test_known_answers_is_empty_on_a_fresh_brain(brain_dir):  # noqa: ARG001
    assert brain.known_answers() == []
