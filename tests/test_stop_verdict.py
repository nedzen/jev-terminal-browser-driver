"""Table of saved finish/stop cases → verdict(). Behaviour must stay identical
except #23 (auto-scroll) and #26 (whole-word / host-excluded evidence)."""

from __future__ import annotations

import pytest

from jev_driver.readiness import (
    DONE_MIN,
    ZERO_ACTION_DONE_MIN,
    Evidence,
    done_acceptable,
    end_state_reached,
    model_action_count,
    verdict,
)

NON_SHELL = "A real paragraph of visible text that is not only short labels."


def _ev(**overrides) -> Evidence:
    base = dict(
        choice="DONE",
        top_op="DONE",
        done_p=0.81,
        shell=False,
        moved_on=False,
        time_budget_spent=False,
        weak_done=0,
        degenerate_streak=0,
        looked=0,
        look_budget=3,
        has_scroll_down=True,
        short_page=False,
        model_history=0,
        end_state=False,
    )
    base.update(overrides)
    return Evidence(**base)


@pytest.mark.parametrize(
    "name, ev, kind",
    [
        ("sev1 zero-action S7b", _ev(done_p=0.81, model_history=0), "reject_done"),
        ("sev1 zero-action S5b", _ev(done_p=0.69, model_history=0), "reject_done"),
        ("already-satisfied S1d", _ev(done_p=1.0, model_history=0), "allow"),
        ("acted DONE at DONE_MIN", _ev(done_p=0.62, model_history=1), "allow"),
        ("second weak DONE stops", _ev(done_p=0.81, model_history=0, weak_done=1), "stop"),
        ("two degenerate strikes", _ev(choice="CLICK", top_op="CLICK", done_p=0.0, degenerate_streak=2), "stop"),
        ("BLOCKED looks further", _ev(choice="BLOCKED", top_op="BLOCKED", done_p=0.0), "look_scroll"),
        (
            "BLOCKED rescue",
            _ev(choice="BLOCKED", top_op="BLOCKED", done_p=0.0, looked=3, end_state=True),
            "rescue_done",
        ),
        ("BLOCKED exhausted no end state", _ev(choice="BLOCKED", top_op="BLOCKED", done_p=0.0, looked=3), "noop"),
        # #23: driver auto-scroll must leave model_history at 0
        ("after auto-scroll still zero-action", _ev(done_p=0.81, model_history=0), "reject_done"),
    ],
)
def test_verdict_table_matches_saved_cases(name, ev, kind):
    assert verdict(ev).kind == kind, name


def test_auto_history_rows_do_not_count_as_model_actions():
    history = [
        {"kind": "scroll", "auto": True, "operation": "SCROLL_DOWN"},
        {"kind": "click", "operation": "CLICK"},
    ]
    assert model_action_count(history) == 1
    assert model_action_count([{"kind": "scroll", "auto": True}]) == 0


def test_zero_action_thresholds_still_separate():
    decision = {"choice": "DONE", "operation_probabilities": {"DONE": 0.81}, "confidence": 0.81}
    page = {"text": NON_SHELL}
    assert not done_acceptable(decision, page, executed_actions=0)
    assert done_acceptable(decision, page, executed_actions=1)
    assert ZERO_ACTION_DONE_MIN > DONE_MIN


def test_s2a_wrong_page_is_not_end_state():
    page = {
        "url": "https://coinmarketcap.com/exchanges/picol",
        "title": "CoinMarketCap Exchanges",
        "text": "Rank Exchange Volume listed for comparison with several liquidity metrics visible.",
    }
    goal = "Click the coin-row link past row 50; done when that coin page is showing."
    history = [{"step": 1, "kind": "click", "operation": "CLICK", "action": "Row 51"}]
    assert end_state_reached(page, goal=goal, history=history, moved_on=True) is False
