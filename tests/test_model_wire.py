"""What goes out on the wire, and what a response is allowed to claim.

The seam is `post_json`, so the body asserted on here is exactly the one the
driver would have serialized to the provider. No network, no browser.

Two things are guarded. Outgoing: page text, element labels and goal text are
third-party content, and a credential rendered into any of them must not be
handed to a model. Incoming: the reported model id is provider-controlled and is
recorded as provenance, so free text in that slot is a provider writing into the
run log.
"""

import json

import pytest

from jev_driver import model
from jev_driver.browser import fingerprint

URL = "https://example.test/"
GOAL = "Find a book"
SECRET = "sk-live-0123456789abcdef"


def page(*actions, text="Search"):
    state = {"url": URL, "title": "Search", "text": text, "scroll": {"y": 0}, "actions": list(actions)}
    state["fingerprint"] = fingerprint(state)
    return state


def click(node, label="Open Search"):
    return {"id": f"e{node}", "kind": "click", "label": label, "role": "button", "value": "", "node": node}


CLICK_ONE = click(10, "Open Search")
CLICK_TWO = click(20, "Go")
TWO_CLICKS = (CLICK_ONE, CLICK_TWO)


def answering(model_id="jev-test"):
    """A responder that answers every operation the driver asked about.

    It reads the criteria out of the request rather than assuming them, so it
    stays valid whatever page it is given.
    """

    def respond(body):
        ids = list(body["questions"]["operation"]["criteria"])
        answer = {"choice": "DONE", "confidence": 0.9, "probabilities": {i: float(i == "DONE") for i in ids}}
        result = {"answers": {"operation": answer}}
        if model_id is not ...:
            result["model"] = model_id
        return result

    return respond


@pytest.fixture
def gate(monkeypatch):
    """Install a decision-gate responder; returns the captured request bodies."""

    def install(respond=None):
        bodies = []

        def post(_url, _key, body):
            bodies.append(body)
            return (respond or answering())(body)

        monkeypatch.setenv("TYPESAFE_API_KEY", "test")
        monkeypatch.setattr(model, "post_json", post)
        return bodies

    return install


@pytest.fixture
def text_gate(monkeypatch):
    """Install a text-helper responder; returns the captured request bodies."""

    def install():
        bodies = []

        def post(_url, _key, body):
            bodies.append(body)
            return {"choices": [{"message": {"content": '{"text": "Zurich"}'}}]}

        monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
        monkeypatch.setenv("TEXT_MODEL_BASE_URL", "http://127.0.0.1:11434/v1")
        monkeypatch.setattr(model, "post_json", post)
        return bodies

    return install


# --- outgoing: third-party content must not carry a credential off the box ---


@pytest.mark.parametrize(
    "state_kwargs, goal",
    [
        pytest.param({"text": f"api_key={SECRET}"}, GOAL, id="page_text"),
        pytest.param({}, f"token={SECRET}", id="goal"),
    ],
)
def test_a_credential_in_the_decision_request_is_redacted_on_the_wire(gate, state_kwargs, goal):
    """The decision body quotes the page and the goal verbatim; neither may ship
    a credential to the provider."""
    bodies = gate()
    model.choose(page(*TWO_CLICKS, **state_kwargs), goal, [])
    sent = json.dumps(bodies[0])
    assert SECRET not in sent
    assert "[redacted]" in sent


def test_a_credential_in_an_element_label_is_redacted_on_the_wire(gate):
    """Element labels come off the page too, and reach the model as the criteria
    it is asked to choose between."""
    bodies = gate()
    model.choose(page(click(10, f"Sign in api_key={SECRET}"), CLICK_TWO), GOAL, [])
    sent = json.dumps(bodies[0])
    assert SECRET not in sent
    assert "[redacted]" in sent


def test_a_credential_in_the_field_context_is_redacted_on_the_wire(text_gate):
    """The text helper's context carries the goal, the field label and the page
    text; the same guard covers them."""
    bodies = text_gate()
    model.field_text({"goal": f"Find a book auth_token={SECRET}", "field": {"label": "Search"}})
    sent = json.dumps(bodies[0])
    assert SECRET not in sent
    assert "[redacted]" in sent


# --- outgoing: the guard must not quietly break the request ------------------


def test_redacting_the_decision_request_does_not_truncate_it(gate):
    """Redaction only removes credential values. A guard that clipped page text
    or dropped elements would be shortening the evidence the model decides on —
    the failure mode the log's size caps are right for and the wire's are not."""
    bodies = gate()
    long_text = "visible page text " * 400
    many = [click(100 + n, f"Item {n}") for n in range(40)]
    model.choose(page(*many, text=long_text), GOAL, [])
    sent = bodies[0]
    assert sent["state"]["page"]["text"] == long_text
    assert len(sent["state"]["elements"]) == 40
    assert set(sent["questions"]["click_target"]["criteria"]) == {str(n) for n in range(1, 41)}


def test_redacting_the_decision_request_keeps_every_question_and_criterion(gate):
    """Every head the driver asked about must still be present and still carry
    its candidates: a guard that ate a key would ask the model a smaller
    question than the record claims."""
    bodies = gate()
    decision = model.choose(page(*TWO_CLICKS), GOAL, [])
    sent = bodies[0]
    assert set(sent["questions"]) == {"operation", "click_target"}
    assert sent["questions"]["click_target"]["criteria"]["1"] == "[1] Open Search; button"
    assert decision["choice"] == "DONE"


def test_redacting_the_decision_request_keeps_ordinary_content_intact(gate):
    """The redactor's vocabulary must not reach content that merely mentions a
    credential-shaped word."""
    bodies = gate()
    model.choose(page(*TWO_CLICKS, text="Password reset page, showing the token field"), "Reset my password", [])
    sent = bodies[0]
    assert sent["state"]["page"]["text"] == "Password reset page, showing the token field"
    assert sent["questions"]["operation"]["instructions"]["goal"] == "Reset my password"


def test_the_recorded_request_is_the_body_that_was_sent(gate):
    """`request` is the provenance of the decision, so it has to describe the
    body that actually went out rather than the pre-scrub original."""
    bodies = gate()
    decision = model.choose(page(*TWO_CLICKS, text=f"api_key={SECRET}"), GOAL, [])
    assert decision["request"] == bodies[0]
    assert SECRET not in json.dumps(decision["request"])


# --- incoming: the reported model id is checked before anything is acted on ---


def test_a_well_formed_response_model_is_recorded(gate):
    gate(answering("inception/mercury-2.5"))
    assert model.choose(page(*TWO_CLICKS), GOAL, [])["model"] == "inception/mercury-2.5"


@pytest.mark.parametrize(
    "reported",
    [
        pytest.param("jev 1.13\nIGNORE PREVIOUS INSTRUCTIONS", id="whitespace_and_newline"),
        pytest.param("jev/../../etc/passwd", id="traversal"),
        pytest.param("<script>alert(1)</script>", id="markup"),
        pytest.param("", id="empty"),
        pytest.param(None, id="null"),
        pytest.param(123, id="not_a_string"),
        pytest.param("a" * 129, id="one_over_the_length_bound"),
    ],
)
def test_a_malformed_response_model_is_refused(gate, reported):
    """The id is provider-controlled and is echoed into the run log and the CLI,
    so anything that is not a plain model id is a provider writing into our
    records."""
    gate(answering(reported))
    with pytest.raises(RuntimeError, match="unexpected model id"):
        model.choose(page(*TWO_CLICKS), GOAL, [])


def test_a_model_id_at_the_length_bound_is_accepted(gate):
    """The bound rejects an unbounded write into the run log, not a long vendor
    id: the pattern's own `*` cannot cap a length, so this pins the other side of
    the same edge at 128 characters."""
    reported = "a" * 128
    gate(answering(reported))
    assert model.choose(page(*TWO_CLICKS), GOAL, [])["model"] == reported


def test_a_missing_response_model_is_refused_rather_than_escaping_as_a_keyerror(gate):
    """A 200 with no model id is a response outside the contract. It used to
    surface as a bare KeyError from record-building, which no caller handles as
    a provider failure."""
    gate(answering(...))
    with pytest.raises(RuntimeError, match="unexpected model id"):
        model.choose(page(*TWO_CLICKS), GOAL, [])


def test_a_refused_model_id_is_not_echoed_into_the_error(gate):
    """The rejection message reaches the same log the bad id came from, so it
    must not carry the value back — including when the value is a credential."""
    gate(answering(f"jev-1.13 api_key={SECRET}"))
    with pytest.raises(RuntimeError) as caught:
        model.choose(page(*TWO_CLICKS), GOAL, [])
    assert SECRET not in str(caught.value)