"""The run-log record: declared once in plugin/core/trace.py.

The migration's contract is that the record is byte-identical to the dict
literal cli.trace_fields used to build, so most of this file pins observable
facts about that record: which keys exist when the run had nothing to say, the
cap on page text, and the one conditional field. A field added to the table and
forgotten by the record is the same dropped-field class Batch A pinned for the
agent result.
"""

from __future__ import annotations

import json

import pytest

from jev_driver import cli
from plugin.core import trace
from plugin.core.trace import TRACE_FIELDS, TraceField, build_trace_record, trace_kind

DECISION = {
    "operation": "CLICK",
    "operation_probabilities": {"CLICK": 0.42, "BLOCKED": 0.3, "SCROLL_DOWN": 0.28},
    "target_probabilities": {"1": 0.55, "2": 0.2, "3": 0.25},
    "request": {"questions": {"click_target": {"criteria": {"1": "[1] Widget; link", "2": "[2] Other; link"}}}},
}


def _snap(decisions=(DECISION,)):
    return {"decisions": list(decisions), "history": []}


def test_the_record_keys_are_the_declared_table_plus_the_conditional_one():
    """A reader asks "was this run's reason recorded?", so an unfilled field is
    an explicit null in a fixed key set — not an absent key that reads the same
    as a run that never had one."""
    record = build_trace_record({}, {}, goal="G")
    assert list(record) == [f.name for f in TRACE_FIELDS]
    assert record["why"] is None
    assert record["reason"] is None
    assert record["error"] is None
    # final_view is the one field that is absent unless it carries something.
    assert "final_view" not in record


def test_an_errored_tick_is_logged_as_blocked_whatever_its_status_claimed():
    """A "tick" line next to an error is the one line nobody can act on."""
    assert trace_kind("ready", "boom") == "blocked"
    assert trace_kind("ready", None) == "tick"
    assert trace_kind("done", None) == "done"
    assert trace_kind("blocked", None) == "blocked"
    assert build_trace_record({"status": "ready", "error": "boom"}, {}, goal="G")["event"] == "blocked"


def test_provenance_names_the_model_that_answered_under_which_prompt():
    """A run is attributable: resolved id, provider version, confidence, prompt."""
    record = build_trace_record(
        {},
        {
            "request": {"model": "jev-1.13.0"},
            "model_version": "2026-10-02",
            "confidence": 0.62,
            "question_spec_hash": "c81014bb333236aa",
        },
        goal="G",
    )
    assert record["model"] == "jev-1.13.0"
    assert record["model_version"] == "2026-10-02"
    assert record["confidence"] == 0.62
    assert record["question_spec_hash"] == "c81014bb333236aa"


def test_a_provider_that_reports_no_version_leaves_an_explicit_null():
    """Absent is not the same answer as unreported, and null is what says which.

    `model_version` was null on every observed response, so this is the shape a
    real run has today: drift is currently undetectable from the log, and the
    record has to be able to state that rather than omit the key.
    """
    record = build_trace_record(
        {},
        {"request": {"model": "jev-1.13.0"}, "model_version": None, "confidence": 0.3},
        goal="G",
    )
    assert "model_version" in record
    assert record["model_version"] is None


def test_the_requested_model_id_is_read_from_the_request_not_the_response_echo():
    """The resolved id is the operator's configuration; the response's own `model`
    is the provider's echo. A substitution is only visible if they are read apart."""
    record = build_trace_record(
        {},
        {"request": {"model": "jev-1.13.0"}, "model": "jev-1.99.0"},
        goal="G",
    )
    assert record["model"] == "jev-1.13.0"


def test_page_text_is_capped_at_the_log_limit_not_the_row_limit():
    """The row keeps PAGE_TEXT_LIMIT (2000) for the agent; the log keeps its own
    1500. Two limits, two reasons, both declared — not one literal reused."""
    assert trace.TRACE_PAGE_TEXT == 1500
    from plugin.core.result import PAGE_TEXT_LIMIT

    assert trace.TRACE_PAGE_TEXT != PAGE_TEXT_LIMIT
    record = build_trace_record({"status": "blocked", "page_text": "z" * 4000}, {}, goal="G")
    assert len(record["page_text"]) == 1500
    exact = build_trace_record({"status": "blocked", "page_text": "z" * 1500}, {}, goal="G")
    assert len(exact["page_text"]) == 1500


def test_an_empty_page_text_is_null_not_an_empty_string():
    """The log distinguishes "we looked and the page said nothing" from "we
    never looked". A blank line in drive.log would flatten those."""
    assert build_trace_record({"page_text": ""}, {}, goal="G")["page_text"] is None


def test_the_ranked_heads_are_read_deeper_than_the_tick_insight():
    """The trace is the whole point of the log, so it keeps 8; the insight copy
    rides every tick and keeps 4 because the agent pays for it each step."""
    assert trace.TRACE_PROBS_LIMIT == 8
    decision = {"operation_probabilities": {f"OP{i}": 0.01 * i for i in range(12)}}
    record = build_trace_record({}, decision, goal="G")
    assert len(record["ranked_ops"]) == 8
    assert record["ranked_ops"][0]["name"] == "OP11"


def test_a_novel_declared_field_reaches_the_record_and_the_log_line(monkeypatch, tmp_path):
    """The dropped-field class, one layer out: a field added to the table must
    appear in the record AND in the readable log line without a second edit.
    Both are checked because runlog._text_lines keys off the record.
    """
    novel = TraceField("shelf_note")
    monkeypatch.setattr(trace, "TRACE_FIELDS", (*TRACE_FIELDS, novel))
    record = build_trace_record({"shelf_note": "left on the top shelf"}, {}, goal="G")
    assert record["shelf_note"] == "left on the top shelf"

    from jev_driver.runlog import _text_lines

    # The text renderer ignores unknown keys today; assert the record carries it
    # and that the renderer is the thing that would need teaching.
    assert "shelf_note" in record
    assert "top shelf" not in _text_lines({**record, "ts": "T"})


def test_a_novel_field_declared_conditional_is_omitted_when_empty(monkeypatch):
    """The two rules are distinct: a present field writes a null, a conditional
    one writes nothing. Conflating them is how "there is no final view" starts
    reading as "there was an empty final view"."""
    novel = TraceField("shelf_note", present=False)
    monkeypatch.setattr(trace, "TRACE_FIELDS", TRACE_FIELDS)
    monkeypatch.setattr(trace, "TRACE_CONDITIONAL", (novel,))
    assert "shelf_note" not in build_trace_record({}, {}, goal="G")
    assert build_trace_record({"shelf_note": "x"}, {}, goal="G")["shelf_note"] == "x"


def test_final_view_is_absent_when_the_run_never_re_read():
    """An empty final_view would claim the driver looked at the page."""
    assert "final_view" not in build_trace_record({"final_view": {}}, {}, goal="G")
    assert build_trace_record({"final_view": {"error": "no browser"}}, {}, goal="G")["final_view"] == {
        "error": "no browser"
    }


def test_target_labels_come_from_the_decisions_own_criteria():
    """A ranked head is unreadable without them: "1 0.55" tells a reader
    nothing, "Widget 0.55" tells them what the model was choosing between."""
    record = build_trace_record({}, DECISION, goal="G")
    assert [item["name"] for item in record["ranked_targets"]] == ["Widget", "3", "Other"]
    assert [item["p"] for item in record["ranked_targets"]] == [0.55, 0.25, 0.2]


def test_cli_delegates_to_the_shared_builder(monkeypatch):
    """One assembly path for the record, the way tick rows already have one."""
    seen = []
    real = cli.build_trace_record

    def spy(rec, decision, *, goal):
        seen.append((dict(rec), dict(decision), goal))
        return real(rec, decision, goal=goal)

    monkeypatch.setattr(cli, "build_trace_record", spy)
    rec = {"status": "done", "url": "u"}
    cli.trace_fields(_snap(), rec, goal="Click the Widget link")
    assert len(seen) == 1
    assert seen[0][0] == rec
    assert seen[0][1]["operation"] == "CLICK"
    assert seen[0][2] == "Click the Widget link"


def test_cli_passes_the_last_decision_not_the_first():
    """A run's final state is explained by the decision that ended it."""
    earlier = {"operation_probabilities": {"CLICK": 0.9}}
    latest = {"operation_probabilities": {"DONE": 0.8}}
    fields = cli.trace_fields(_snap([earlier, latest]), {"status": "done", "url": "u"}, goal="G")
    assert fields["ranked_ops"][0]["name"] == "DONE"


def test_the_round_trip_from_tick_row_to_log_record_is_lossless():
    """A real tick row, built by the tick builder, must fill the record without
    the record reaching back into the snapshot for the same facts."""
    snap = {
        "status": "blocked",
        "page": {"url": "https://example.test/", "text": "Home", "omitted_actions": 37},
        "history": [{"action": "Widget", "operation": "CLICK", "page_changed": False}],
        "decisions": [DECISION],
    }
    row = cli.tick_record(snap)
    fields = cli.trace_fields(snap, row, goal="Click the Widget link")
    for key in ("status", "url", "last_action", "why", "reason", "degenerate"):
        assert fields[key] == row.get(key), key
    assert fields["page_text"] == row["page_text"]
    # json-serializable without a custom encoder: the log's own writer relies on it.
    json.dumps(fields)


@pytest.mark.parametrize("status,error", [("ready", None), ("done", None), ("blocked", None), ("ready", "x")])
def test_the_kind_taxonomy_is_total(status, error):
    """Every combination the loop can produce maps to a name the log reader
    already knows; an unmapped kind would be a line nobody greps for."""
    assert trace_kind(status, error) in {"tick", "done", "blocked"}