"""Tab reuse decision and the on-disk run log. No live browser."""

import json

from jev_driver.cli import choose_lease, trace_fields
from jev_driver.runlog import write_event


def test_url_navigates_the_existing_driver_tab():
    plan = choose_lease(
        url="https://example.test/next",
        target_id=None,
        continuable=("T1", "https://example.test/now"),
        default_url="file:///fixture",
    )
    assert plan["tab"] == "target"
    assert plan["target_id"] == "T1"
    assert plan["navigate"] is True
    assert plan["agent_url"] == "https://example.test/next"
    assert plan["continuity"] == "re-attach"


def test_missing_tab_is_the_only_new_tab():
    plan = choose_lease(
        url="https://example.test/next",
        target_id=None,
        continuable=(None, None),
        default_url="file:///fixture",
        dropped="dropped:stale-id",
    )
    assert plan["tab"] == "new"
    assert plan["navigate"] is True
    assert plan["agent_url"] == "https://example.test/next"


def test_omit_url_stays_on_the_same_tab_without_navigating():
    plan = choose_lease(
        url=None,
        target_id=None,
        continuable=("T1", "https://example.test/now"),
        default_url="file:///fixture",
    )
    assert plan["tab"] == "target"
    assert plan["navigate"] is False
    assert plan["agent_url"] == "https://example.test/now"


def test_explicit_target_is_not_replaced_by_the_remembered_tab():
    plan = choose_lease(
        url="https://example.test/next",
        target_id="EXPLICIT",
        continuable=("T1", "https://example.test/now"),
        default_url="file:///fixture",
        navigate_explicit=False,
    )
    assert plan["target_id"] == "EXPLICIT"
    assert plan["navigate"] is False


def test_blocked_trace_keeps_goal_and_ranked_hits():
    snap = {
        "decisions": [
            {
                "operation": "CLICK",
                "operation_probabilities": {"CLICK": 0.42, "BLOCKED": 0.4, "SCROLL_DOWN": 0.18},
                "target_probabilities": {"1": 0.55, "2": 0.45},
                "request": {
                    "questions": {
                        "click_target": {
                            "criteria": {"1": "[1] Widget; link", "2": "[2] Unrelated; link"}
                        }
                    }
                },
            }
        ]
    }
    rec = {
        "status": "blocked",
        "url": "https://example.test/",
        "last_action": "Widget",
        "why": "Stopped: three actions in a row left the page unchanged.",
        "page_text": "Home",
    }
    fields = trace_fields(snap, rec, goal="Click the Widget link")
    assert fields["event"] == "blocked"
    assert fields["goal"] == "Click the Widget link"
    assert fields["ranked_ops"][0] == {"name": "CLICK", "p": 0.42}
    assert fields["ranked_targets"][0] == {"name": "Widget", "p": 0.55}
    assert fields["ranked_targets"][1]["name"] == "Unrelated"


def test_log_writes_goal_ranks_and_block(tmp_path):
    jsonl = tmp_path / "drive.jsonl"
    text = tmp_path / "drive.log"
    write_event(
        {
            "event": "run",
            "goal": "Click the Widget link",
            "url": "https://example.test/",
            "continuity": "re-attach",
        },
        jsonl_path=jsonl,
        text_path=text,
    )
    write_event(
        {
            "event": "blocked",
            "goal": "Click the Widget link",
            "status": "blocked",
            "why": "Model chose BLOCKED: no supported operation can progress this goal.",
            "url": "https://example.test/stuck",
            "ranked_ops": [{"name": "BLOCKED", "p": 0.7}, {"name": "CLICK", "p": 0.3}],
            "ranked_targets": [{"name": "Widget", "p": 0.4}],
            "page_text": "nothing useful here",
        },
        jsonl_path=jsonl,
        text_path=text,
    )
    rows = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert rows[0]["goal"] == "Click the Widget link"
    assert rows[0]["continuity"] == "re-attach"
    assert rows[1]["event"] == "blocked"
    assert rows[1]["ranked_ops"][0]["name"] == "BLOCKED"
    body = text.read_text()
    assert "Click the Widget link" in body
    assert "BLOCKED 0.7" in body
    assert "Widget 0.4" in body
    assert "nothing useful here" in body
