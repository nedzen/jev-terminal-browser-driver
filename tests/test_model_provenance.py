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
    # Both are named as skipped, so "mixed" can be checked without replaying the
    # request: the head this decision used is `model`, the two it did not are
    # `not-executed`, and both are ours.
    assert decision["stages"]["CLICK"] == {"backend": "typesafe", "source": "model"}
    assert decision["stages"]["SELECT"] == {"backend": "deterministic", "source": "not-executed"}
    assert decision["stages"]["TYPE_TEXT"] == {"backend": "deterministic", "source": "not-executed"}
    # Insertion order, not just membership: the run log serialises the dict as it
    # stands, so a set-order walk would make the same decision log differently on a
    # different interpreter run.
    assert list(decision["stages"]) == ["operation", "CLICK", "SELECT", "TYPE_TEXT"]


def test_an_unexecuted_bypass_carries_no_numbers_at_all(monkeypatch):
    """A head nobody asked has no target, no confidence and no probabilities: a
    synthetic 1.0 there would be the one thing a calibrator could not tell from a
    model's answer."""
    decision, _calls = decide(
        monkeypatch,
        page(FILL, CLICK_ONE, CLICK_TWO),
        {"operation": choice(["TYPE_TEXT", "CLICK", "DONE", "BLOCKED"], "CLICK"),
         "click_target": choice(["1", "2"], "1")},
    )
    stage = decision["stages"]["TYPE_TEXT"]
    assert set(stage) == {"backend", "source"}
    assert "target" not in stage and "confidence" not in stage and "probabilities" not in stage
    # The executed head is still fully described, so the map is not uniform noise.
    assert decision["stages"]["CLICK"]["backend"] == "typesafe"


def test_the_executed_bypass_is_not_overwritten_by_a_not_executed_marker(monkeypatch):
    decision, _calls = decide(
        monkeypatch,
        MIXED_PAGE,
        {"operation": choice(["TYPE_TEXT", "CLICK", "WAIT", "DONE", "BLOCKED"], "TYPE_TEXT")},
    )
    assert decision["stages"]["TYPE_TEXT"]["source"] == "bypass"  # not "not-executed"
    assert decision["stages"]["TYPE_TEXT"]["target"] == "1"


def test_a_control_operation_still_reports_every_bypassed_head(monkeypatch):
    # SCROLL has no target head at all, and DONE/BLOCKED are never bypassed, so
    # this is the case where nothing but the record says a bypass existed.
    scroll = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "node": None}
    decision, _calls = decide(
        monkeypatch,
        page(FILL, scroll),
        {"operation": choice(["TYPE_TEXT", "SCROLL_DOWN", "DONE", "BLOCKED"], "SCROLL_DOWN")},
    )
    assert decision["operation"] == "SCROLL_DOWN"
    assert decision["decision_source"] == "mixed"
    assert decision["bypassed_heads"] == ["TYPE_TEXT"]
    assert decision["stages"]["TYPE_TEXT"] == {"backend": "deterministic", "source": "not-executed"}
    assert set(decision["stages"]) == {"TYPE_TEXT", "operation"}  # no invented SCROLL stage


def test_no_bypass_anywhere_means_no_stages_but_the_ones_asked(monkeypatch):
    decision, _calls = decide(
        monkeypatch,
        page(CLICK_ONE, CLICK_TWO),
        {"operation": choice(["CLICK", "DONE", "BLOCKED"], "CLICK"),
         "click_target": choice(["1", "2"], "1")},
    )
    assert decision["decision_source"] == "model"
    assert decision["bypassed_heads"] == []
    assert sorted(decision["stages"]) == ["CLICK", "operation"]
    assert all(stage["backend"] == "typesafe" for stage in decision["stages"].values())


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


# --- keyless operation: only against an explicitly configured endpoint -------


NO_KEY_ENV = ("TYPESAFE_API_KEY", "DECISION_GATE_API_KEY", "OPENROUTER_API_KEY", "TEXT_MODEL_API_KEY")


@pytest.fixture
def keyless(monkeypatch):
    """No credential anywhere in the environment or the env files."""
    for name in NO_KEY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(model, "_key_from_env_files", lambda names: None)


def test_a_keyless_call_to_a_configured_decision_gate_is_allowed(monkeypatch, keyless):
    """`DECISION_GATE_URL` is the operator naming a server and accepting its
    contract, which is the only case where an unauthenticated request is a local
    convenience rather than a request to a third party."""
    monkeypatch.setenv("DECISION_GATE_URL", "http://127.0.0.1:8080/v1/systemone")
    calls = []

    def post(url, key, body):
        calls.append((url, key))
        return {"model": "local", "answers": {"operation": choice(["CLICK", "DONE", "BLOCKED"], "DONE")}}

    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose(page(CLICK_ONE, CLICK_TWO), GOAL, [])

    assert calls == [("http://127.0.0.1:8080/v1/systemone", None)]  # the key is absent, not empty
    assert decision["choice"] == "DONE"
    assert decision["backend"] == "typesafe"


def test_a_keyless_call_to_the_hosted_gate_is_refused(monkeypatch, keyless):
    """No explicit endpoint means the URL is the derived hosted default, so a
    missing credential is the operator's to fix rather than something to send
    unauthenticated."""
    def post(*args, **kwargs):
        raise AssertionError("nothing may be sent without a key or a configured endpoint")

    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        model.choose(page(CLICK_ONE, CLICK_TWO), GOAL, [])


def test_a_blank_endpoint_is_not_an_explicit_endpoint(monkeypatch, keyless):
    """`DECISION_GATE_URL=""` configures nothing; treating it as permission would
    make the flag look like it worked."""
    monkeypatch.setenv("DECISION_GATE_URL", "   ")
    assert model.explicit_endpoint("DECISION_GATE_URL") is None
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        model.choose(page(CLICK_ONE, CLICK_TWO), GOAL, [])


def test_a_configured_endpoint_with_a_key_still_sends_it(monkeypatch):
    """The keyless path is an allowance, not a replacement: an explicit endpoint
    never stops a credential from being used."""
    monkeypatch.setenv("DECISION_GATE_URL", "http://127.0.0.1:8080/v1/systemone")
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-live-SECRET")
    seen = []

    def post(url, key, body):
        seen.append((url, key))
        return {"model": "local", "answers": {"operation": choice(["CLICK", "DONE", "BLOCKED"], "DONE")}}

    monkeypatch.setattr(model, "post_json", post)
    model.choose(page(CLICK_ONE, CLICK_TWO), GOAL, [])
    assert seen == [("http://127.0.0.1:8080/v1/systemone", "sk-live-SECRET")]


def test_a_keyless_text_helper_call_needs_a_configured_endpoint(monkeypatch, keyless):
    def post(*args, **kwargs):
        raise AssertionError("nothing may be sent without a key or a configured endpoint")

    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        model.field_text({"goal": GOAL})
    assert model.explicit_endpoint("TEXT_MODEL_BASE_URL") is None


def test_a_keyless_text_helper_works_against_a_configured_endpoint(monkeypatch, keyless):
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "http://127.0.0.1:11434/v1/")
    seen = []

    def post(url, key, body):
        seen.append((url, key))
        return {"choices": [{"message": {"content": '{"text": "Zurich"}'}}]}

    monkeypatch.setattr(model, "post_json", post)
    value, meta = model.field_text({"goal": GOAL})
    assert value == "Zurich"
    assert seen == [("http://127.0.0.1:11434/v1/chat/completions", None)]
    assert meta["model"] == "inception/mercury-2.5"


def test_a_keyed_text_helper_call_is_unchanged(monkeypatch, keyless):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "sk-live-SECRET")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "http://127.0.0.1:11434/v1")
    seen = []

    def post(url, key, body):
        seen.append((url, key))
        return {"choices": [{"message": {"content": '{"text": "Zurich"}'}}]}

    monkeypatch.setattr(model, "post_json", post)
    assert model.field_text({"goal": GOAL})[0] == "Zurich"
    assert seen == [("http://127.0.0.1:11434/v1/chat/completions", "sk-live-SECRET")]


def test_a_deepseek_base_still_switches_the_reasoning_block(monkeypatch, keyless):
    # The endpoint variable is read for two things now: permission to be keyless
    # and the deepseek reasoning shape. It must still do the second.
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "sk-live-SECRET")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "1")
    bodies = []

    def post(url, key, body):
        bodies.append(body)
        return {"choices": [{"message": {"content": '{"text": "Zurich"}'}}]}

    monkeypatch.setattr(model, "post_json", post)
    model.field_text({"goal": GOAL})
    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert "reasoning" not in bodies[0]


def test_empty_key_retries_still_omit_the_header(monkeypatch):
    post = Mock(side_effect=[httpx.Response(503, json={"error": "busy"}), httpx.Response(200, json={"ok": True})])
    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert model.post_json("http://127.0.0.1:8080/v1/systemone", "", {}) == {"ok": True}
    assert post.call_count == 2
    for call in post.call_args_list:
        assert not call.kwargs["headers"]
