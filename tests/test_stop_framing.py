"""The DONE stop-framing contract: what counts as observable end-state evidence.

The payloads below are frozen from the V2 score-only probe (no browser, no live
drive): one frozen page state -- the IANA end state the failing release-gate runs
reached -- scored twice, varying only the goal string. Recorded under the
pre-framing prompt, question_spec_hash c81014bb333236aa.

A = the release-gate wording that ended BLOCKED.
B = the wording that names its end state, which ended DONE.

The probe is not re-run here: it needs the live decision backend. These tests
pin the threshold behaviour that framing is supposed to move, and pin the
provenance of the recordings so a prompt edit forces a re-measurement.
"""

from jev_driver.drive_agent import _top_operation
from jev_driver.model import QUESTION_SPEC_HASH
from jev_driver.questions import NEXT_ACTION
from jev_driver.readiness import DONE_MIN, done_acceptable, done_probability, page_is_shell

# The frozen IANA end state, verbatim from the run log's excerpt (drive.jsonl,
# 2026-10-02T22:49:33Z blocked event). Truncated there at ~200 chars, but the
# tail is a real sentence, so the excerpt already clears the shell guard.
IANA_TEXT = (
    "Domains\nProtocols\nNumbers\nAbout\nInstructions and Guides\nExample Domains\n"
    "A number of domains such as\nexample.com\nand\nexample.org\nare maintained\n"
    "for documentation purposes. These domains may be used as…"
)
IANA_URL = "https://www.iana.org/help/example-domains"
IANA_PAGE = {"url": IANA_URL, "title": "", "text": IANA_TEXT}

# Recorded live probe answers. `operation_probabilities` is the operation head
# verbatim, under the key model.py:457 puts it under.
THEN_STOP = {
    "goal": "Go to example.com and click the 'More information...' / 'Learn more' link, then stop.",
    "operation_probabilities": {"DONE": 0.2, "BLOCKED": 0.8},
    "done_p_range": (0.17, 0.22),
}
NAMED_END_STATE = {
    "goal": "Click the Learn more link; done when the IANA example domains page shows",
    "operation_probabilities": {"DONE": 0.64, "BLOCKED": 0.36},
    "done_p_range": (0.62, 0.67),
}

# The spec hash the recordings above were taken under.
RECORDED_SPEC_HASH = "c81014bb333236aa"


def test_done_criterion_admits_the_observed_url_and_history_as_evidence():
    """The DONE clause must name the current URL/title and past actions as evidence."""
    lowered = NEXT_ACTION.lower()
    assert "that evidence may be the current" in lowered
    assert "url and title" in lowered
    assert "what your recent actions already did" in lowered


def test_done_criterion_says_an_already_performed_action_that_moved_the_page_satisfies_the_goal():
    """A goal needing only an action already done is satisfied by that action changing the page."""
    assert "action you have already performed is satisfied once" in NEXT_ACTION
    assert "even when the new page repeats nothing from the goal" in NEXT_ACTION


def test_matching_link_alone_remains_insufficient_for_done():
    """The pre-existing rule must survive the appended clause, not be replaced by it."""
    assert "a matching link is not enough" in NEXT_ACTION


def test_then_stop_frozen_payload_stays_below_the_done_threshold():
    """'then stop' with no named end state scored DONE under the threshold on every sample."""
    lo, hi = THEN_STOP["done_p_range"]
    assert hi < DONE_MIN
    assert not done_acceptable(THEN_STOP, IANA_PAGE)
    assert done_probability(THEN_STOP) == THEN_STOP["operation_probabilities"]["DONE"]


def test_named_end_state_frozen_payload_clears_the_done_threshold():
    """A goal naming its end state cleared DONE_MIN on every sample."""
    lo, hi = NAMED_END_STATE["done_p_range"]
    assert lo >= DONE_MIN
    assert done_acceptable(NAMED_END_STATE, IANA_PAGE)


def test_the_frozen_end_state_clears_the_shell_gate():
    """The end state carried a sentence, so DONE_MIN was the only binding gate."""
    assert not page_is_shell(IANA_TEXT)


def test_a_then_stop_blocked_choice_is_beyond_the_moved_on_bypass():
    """BLOCKED on top means the drive_agent.py:591 bypass cannot reach it: it needs DONE on top."""
    assert _top_operation(THEN_STOP) == "BLOCKED"
    assert _top_operation(NAMED_END_STATE) == "DONE"


def test_frozen_calibration_payloads_predate_the_stop_framing_change():
    """Tripwire: the DONE framing moved, so these recordings must be re-measured.

    Reverting the framing restores the recorded hash and fails this test.
    """
    assert RECORDED_SPEC_HASH != QUESTION_SPEC_HASH
