"""Provenance for every decision: which prompts, which model, which stage decided.

Mocked transport, no paid calls, no browser.
"""

import hashlib
import time
from unittest.mock import Mock

import httpx
import pytest

from jev_driver import model
from jev_driver.browser import fingerprint
from jev_driver.questions import NEXT_ACTION, TARGET, TEXT_VALUE

URL = "https://example.test/"
GOAL = "Find a book"


def page(*actions):
    state = {
        "url": URL,
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": list(actions),
    }
    state["fingerprint"] = fingerprint(state)
    return state


FILL = {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10}
CLICK_ONE = {"id": "e2", "kind": "click", "label": "Open Search", "role": "button", "value": "", "node": 10}
CLICK_TWO = {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20}
WAIT = {"id": "wait", "kind": "wait", "label": "Wait"}
# TYPE_TEXT has one candidate, CLICK has two: the mixed decision_source case.
MIXED_PAGE = page(FILL, CLICK_ONE, CLICK_TWO, WAIT)
# Every head has one candidate, yet the operation head always calls the model.
SOLE_PAGE = page(FILL)


def choice(ids, selected, confidence=0.9):
    """A well-formed answer the way the backend returns one."""
    return {"choice": selected, "confidence": confidence, "probabilities": {i: float(i == selected) for i in ids}}


def backend(answers, usage=None):
    """A model double that never answers a question the driver did not ask."""

    def post(_url, _key, body):
        asked = body["questions"]
        for name in answers:
            assert name in asked, f"question {name!r} was dropped from the request"
        result = {"model": "jev-test", "answers": dict(answers)}
        if usage is not None:
            result["usage"] = usage
        return result

    return post


def decide(monkeypatch, state, answers, usage=None):
    calls = []

    def post(url, key, body):
        calls.append((url, key, body))
        return backend(answers, usage)(url, key, body)

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose(state, GOAL, [])
    return decision, calls


def test_hash_is_sixteen_hex_chars_over_the_shipped_prompts():
    assert model.QUESTION_SPEC_HASH == model.question_spec_hash()
    assert len(model.QUESTION_SPEC_HASH) == 16
    assert all(c in "0123456789abcdef" for c in model.QUESTION_SPEC_HASH)


def test_hash_pins_the_named_prompt_encoding():
    # A golden digest: the hash is a contract between records and this spec
    # revision, so the encoding (names included, order fixed) is pinned here and
    # cannot drift silently. Recomputed independently, not read from model.py.
    names = ("NEXT_ACTION", "TARGET", "TEXT_VALUE")
    prompts = (NEXT_ACTION, TARGET, TEXT_VALUE)
    blob = "".join(f"{name}\x1f{prompt}\x1e" for name, prompt in zip(names, prompts))
    expected = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    assert model.QUESTION_SPEC_HASH == expected
    # Names are part of the digest: two questions cannot be swapped into one
    # another's slot without the hash noticing.
    swapped = "".join(f"{name}\x1f{prompt}\x1e" for name, prompt in zip(names, ("TARGET", "NEXT_ACTION", "TEXT_VALUE")))
    assert hashlib.sha256(swapped.encode("utf-8")).hexdigest()[:16] != expected


def test_hash_is_stable_and_tracks_each_prompt():
    baseline = model.question_spec_hash([NEXT_ACTION, TARGET, TEXT_VALUE])
    assert baseline == model.QUESTION_SPEC_HASH
    for changed in (
        [NEXT_ACTION + " Extra rule.", TARGET, TEXT_VALUE],
        [NEXT_ACTION, TARGET + " Extra rule.", TEXT_VALUE],
        [NEXT_ACTION, TARGET, TEXT_VALUE + " Extra rule."],
        # A swap must not hash like the original: names are part of the digest.
        [TARGET, NEXT_ACTION, TEXT_VALUE],
    ):
        assert model.question_spec_hash(changed) != baseline


def test_hash_rejects_a_wrong_number_of_prompts():
    with pytest.raises(ValueError, match="3 prompts"):
        model.question_spec_hash([NEXT_ACTION, TARGET])


def test_every_decision_carries_model_backend_and_prompt_provenance(monkeypatch):
    decision, _calls = decide(
        monkeypatch,
        MIXED_PAGE,
        {"operation": choice(["TYPE_TEXT", "CLICK", "WAIT", "DONE", "BLOCKED"], "CLICK"),
         "click_target": choice(["1", "2"], "1")},
    )
    assert decision["backend"] == "typesafe"
    assert decision["decision_source"] == "mixed"
    assert decision["calibration_surface"] == "typesafe_cloud"
    assert decision["question_spec_hash"] == model.QUESTION_SPEC_HASH
    assert decision["model"] == "jev-test"
    # No version reported means unknown, never inferred from the model name.
    assert decision["model_version"] is None
    assert decision["stages"]["operation"] == {"backend": "typesafe", "source": "model"}
    assert decision["stages"]["CLICK"] == {"backend": "typesafe", "source": "model"}


def test_reported_model_version_is_recorded_verbatim(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "jev-1.13.0",
            "model_version": "2026-09-30",
            "answers": {"operation": choice(["TYPE_TEXT", "CLICK", "WAIT", "DONE", "BLOCKED"], "WAIT")},
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    assert model.choose(MIXED_PAGE, GOAL, [])["model_version"] == "2026-09-30"


def test_a_one_candidate_head_is_never_asked(monkeypatch):
    decision, calls = decide(
        monkeypatch,
        MIXED_PAGE,
        {"operation": choice(["TYPE_TEXT", "CLICK", "WAIT", "DONE", "BLOCKED"], "CLICK"),
         "click_target": choice(["1", "2"], "1")},
    )
    assert set(calls[0][2]["questions"]) == {"operation", "click_target"}
    # The operation choice still advertises the bypassed operation.
    assert "TYPE_TEXT" in calls[0][2]["questions"]["operation"]["criteria"]


def test_bypassed_head_is_tagged_deterministic_and_mixed(monkeypatch):
    decision, _calls = decide(
        monkeypatch,
        MIXED_PAGE,
        {"operation": choice(["TYPE_TEXT", "CLICK", "WAIT", "DONE", "BLOCKED"], "TYPE_TEXT")},
    )
    assert decision["operation"] == "TYPE_TEXT"
    assert decision["target"] == "1" and decision["choice"] == "e1"
    assert decision["probabilities"] == {"e1": 1.0}
    assert decision["target_probabilities"] == {"1": 1.0}
    assert decision["target_confidence"] == 1.0
    # The operation confidence is still the model's, never a synthetic 1.0.
    assert decision["confidence"] == 0.9
    assert decision["backend"] == "deterministic"
    assert decision["decision_source"] == "mixed"
    assert decision["bypassed_heads"] == ["TYPE_TEXT"]
    assert decision["stages"]["TYPE_TEXT"] == {
        "backend": "deterministic",
        "source": "bypass",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {"1": 1.0},
    }
    # Calibration discipline: the one head that produced numbers is identified.
    assert [head for head, stage in decision["stages"].items() if stage["backend"] == "deterministic"] == ["TYPE_TEXT"]


def test_a_bypassed_head_cannot_be_overridden_by_a_hedged_model_answer(monkeypatch):
    # Even if a backend volunteers an answer for the unasked head, the bypassed
    # target stands: it is the only legal candidate anyway.
    def post(_url, _key, body):
        return {
            "model": "jev-test",
            "answers": {
                "operation": choice(["TYPE_TEXT", "CLICK", "WAIT", "DONE", "BLOCKED"], "TYPE_TEXT"),
                "type_text_target": {"choice": "1", "confidence": 0.5, "probabilities": {"1": 0.4, "9": 0.6}},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose(MIXED_PAGE, GOAL, [])
    assert decision["target"] == "1"
    assert decision["target_confidence"] == 1.0
    assert decision["stages"]["TYPE_TEXT"]["backend"] == "deterministic"


def test_bypassed_heads_are_sorted_and_complete(monkeypatch):
    # Two one-candidate heads (TYPE_TEXT, SELECT) plus a two-candidate CLICK.
    select_actions = [
        {"id": "s1", "kind": "select", "label": "Design → Layout", "role": "combobox", "value": "Layout", "node": 30}
    ]
    decision, calls = decide(
        monkeypatch,
        page(FILL, CLICK_ONE, CLICK_TWO, *select_actions),
        {"operation": choice(["TYPE_TEXT", "SELECT", "CLICK", "DONE", "BLOCKED"], "CLICK"),
         "click_target": choice(["1", "2"], "1")},
    )
    assert set(calls[0][2]["questions"]) == {"operation", "click_target"}
    # Sorted, not set order: a recorded decision must read the same twice.
    assert decision["bypassed_heads"] == ["SELECT", "TYPE_TEXT"]
    assert decision["decision_source"] == "mixed"


def test_a_multi_candidate_head_stays_a_model_decision(monkeypatch):
    decision, calls = decide(
        monkeypatch,
        MIXED_PAGE,
        {"operation": choice(["TYPE_TEXT", "CLICK", "WAIT", "DONE", "BLOCKED"], "CLICK"),
         "click_target": choice(["1", "2"], "2", confidence=0.8)},
    )
    assert "click_target" in calls[0][2]["questions"]
    assert decision["target"] == "2" and decision["choice"] == "e3"
    assert decision["target_confidence"] == 0.8
    assert decision["target_probabilities"] == {"1": 0.0, "2": 1.0}
    assert decision["stages"]["CLICK"] == {"backend": "typesafe", "source": "model"}
    # A bypass on another head does not relabel this decision's target.
    assert decision["backend"] == "typesafe"
    assert decision["decision_source"] == "mixed"
    assert decision["bypassed_heads"] == ["TYPE_TEXT"]


def test_no_bypass_anywhere_is_a_pure_model_decision(monkeypatch):
    two_fields = page(FILL, {**FILL, "id": "e4", "node": 11})
    decision, calls = decide(
        monkeypatch,
        two_fields,
        {"operation": choice(["TYPE_TEXT", "DONE", "BLOCKED"], "TYPE_TEXT"),
         "type_text_target": choice(["1", "2"], "2", confidence=0.7)},
    )
    assert set(calls[0][2]["questions"]) == {"operation", "type_text_target"}
    assert decision["backend"] == "typesafe"
    assert decision["decision_source"] == "model"
    assert decision["bypassed_heads"] == []
    assert all(stage["backend"] == "typesafe" for stage in decision["stages"].values())


def test_every_head_bypassed_still_costs_one_typed_model_call(monkeypatch):
    decision, calls = decide(
        monkeypatch,
        SOLE_PAGE,
        {"operation": choice(["TYPE_TEXT", "DONE", "BLOCKED"], "TYPE_TEXT", confidence=0.6)},
        usage={"input_tokens": 11, "output_tokens": 2, "cost": 0.0004},
    )
    # Nothing is fully deterministic: DONE and BLOCKED keep the operation open.
    assert len(calls) == 1
    assert set(calls[0][2]["questions"]) == {"operation"}
    assert decision["decision_source"] == "mixed"
    assert decision["bypassed_heads"] == ["TYPE_TEXT"]
    assert decision["usage"] == {"input_tokens": 11, "output_tokens": 2, "cost": 0.0004}


def test_usage_is_recorded_for_a_pure_model_decision(monkeypatch):
    decision, _calls = decide(
        monkeypatch,
        page(CLICK_ONE, CLICK_TWO),
        {"operation": choice(["CLICK", "DONE", "BLOCKED"], "CLICK"),
         "click_target": choice(["1", "2"], "1")},
        usage={"input_tokens": 5, "output_tokens": 1, "cost": 0.0002},
    )
    assert decision["decision_source"] == "model"
    assert decision["usage"] == {"input_tokens": 5, "output_tokens": 1, "cost": 0.0002}





@pytest.mark.parametrize(
    "probabilities", [{"a": 0.55, "b": 0.42}, {"a": 0.55, "b": 0.45}, {"a": 0.7, "b": 0.34}]
)
def test_probability_sums_within_tolerance_are_accepted(probabilities):
    answer = {"choice": "a", "confidence": 0.9, "probabilities": probabilities}
    assert model.validate_choice(answer, {"a", "b"})["choice"] == "a"


@pytest.mark.parametrize("probabilities", [{"a": 0.8, "b": 0.25}, {"a": 0.9, "b": 0.0, "c": 0.0}])
def test_probability_sums_beyond_tolerance_are_rejected(probabilities):
    ids = set(probabilities)
    answer = {"choice": "a", "confidence": 0.9, "probabilities": probabilities}
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(answer, ids)


def test_decision_key_is_accepted_for_the_chosen_option():
    answer = {"decision": "a", "confidence": 0.9, "probabilities": {"a": 0.6, "b": 0.4}}
    validated = model.validate_choice(answer, {"a", "b"})
    assert validated["choice"] == "a"
    assert validated["decision"] == "a"
    # raw_answers keep the backend's own spelling; normalization is a copy.
    assert "choice" not in answer


def test_decision_key_is_validated_as_strictly_as_choice():
    # Accepted spelling, same rejections as `choice`: unknown option, a
    # probability set that does not cover the ids, a non-maximal pick, and a
    # missing confidence.
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice({"decision": "invented", "confidence": 1.0, "probabilities": {"a": 1.0}}, {"a"})
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice({"decision": "a", "confidence": 1.0, "probabilities": {"a": 0.6}}, {"a"})
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice({"decision": "a", "confidence": 1.0}, {"a"})
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice({"decision": "a", "confidence": 1.0, "probabilities": {"a": 0.4, "b": 0.6}}, {"a", "b"})


def test_agreeing_dual_keys_pass_and_disagreeing_ones_do_not():
    both = {"choice": "a", "decision": "a", "confidence": 0.9, "probabilities": {"a": 1.0}}
    assert model.validate_choice(both, {"a"})["choice"] == "a"
    conflicting = {"choice": "a", "decision": "b", "confidence": 0.9, "probabilities": {"a": 1.0}}
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(conflicting, {"a"})


def test_dual_key_answers_flow_through_choose(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "jev-test",
            "answers": {
                "operation": {
                    "decision": "CLICK",
                    "confidence": 0.75,
                    "probabilities": {i: float(i == "CLICK") for i in body["questions"]["operation"]["criteria"]},
                },
                "click_target": {"decision": "2", "confidence": 0.75, "probabilities": {"1": 0.0, "2": 1.0}},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose(page(CLICK_ONE, CLICK_TWO), GOAL, [])
    assert decision["choice"] == "e3" and decision["target"] == "2"
    assert decision["decision_source"] == "model"


@pytest.mark.parametrize("key", ["", None])
def test_an_empty_key_sends_no_authorization_header(monkeypatch, key):
    post = Mock(return_value=httpx.Response(200, json={"ok": True}))
    monkeypatch.setattr(model.CLIENT, "post", post)
    assert model.post_json("http://127.0.0.1:8080/v1/systemone", key, {}) == {"ok": True}
    sent = post.call_args.kwargs["headers"]
    assert not any(name.lower() == "authorization" for name in sent or ())


def test_a_real_key_still_sends_the_bearer_header(monkeypatch):
    post = Mock(return_value=httpx.Response(200, json={"ok": True}))
    monkeypatch.setattr(model.CLIENT, "post", post)
    model.post_json("https://api.typesafe.ai/v1/systemone", "secret", {})
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}


def test_empty_key_retries_still_omit_the_header(monkeypatch):
    post = Mock(side_effect=[httpx.Response(503, json={"error": "busy"}), httpx.Response(200, json={"ok": True})])
    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert model.post_json("http://127.0.0.1:8080/v1/systemone", "", {}) == {"ok": True}
    assert post.call_count == 2
    for call in post.call_args_list:
        assert not call.kwargs["headers"]