"""Self-tests for the live harness, against a scripted MCP server.

No browser, no pane, no network: every outcome is scripted by
`scripts/live/fake_server.py`. What is being tested is the harness's own claims --
that it classifies all four v3 outcomes correctly, that it refuses a busy pane,
that a stall is noticed from the log rather than from the socket, and that
redaction removes amounts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.live import chain as chain_mod
from scripts.live import isolation, runner, taxonomy
from scripts.live.classify import (
    classify,
    confidence_inversion,
    end_state_matched,
    host_and_path,
)
from scripts.live.driver import McpStdio
from scripts.live.fake_server import (
    CONSEQUENTIAL_HIT,
    CRASH,
    FALSE_DONE,
    FALSE_DONE_SEV1,
    HIT,
    HIT_AT_PREFIX,
    HIT_RECOVERED,
    HONEST_BLOCKED,
    ISOLATION_VIOLATION,
    NEVER_RETURNS,
    PAGE_TEXT_AT_PREFIX,
    STALL_SILENT,
    UNJUSTIFIED_BLOCKED,
    spawn,
)
from scripts.live.metrics import (
    CallMeter,
    consequential_hits,
    count_ticks,
    ranked_targets_empty_rate,
    result_bytes,
    waste_ticks,
)
from scripts.live.redact import assert_no_amounts, presence_only, redact_record
from scripts.live.spec import ManifestError, validate_manifest, validate_test

IANA = "https://www.iana.org/help/example-domains"


def _test(**over):
    base = {
        "id": "S1a", "site": "example", "goal": "Click Learn more", "tier": "R",
        "satisfiable": True,
        "expected": {"url_host_path": "www.iana.org/help/example-domains"},
        "hit_line": "coin page showing", "miss_line": "anything else",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# Manifest schema
# --------------------------------------------------------------------------


def test_a_manifest_defaults_the_timeout_to_300_seconds():
    """v3: per-test timeout_s, default 300."""
    assert validate_test(_test())["timeout_s"] == 300


def test_a_per_test_timeout_overrides_the_default():
    assert validate_test(_test(timeout_s=45))["timeout_s"] == 45


def test_an_unknown_tier_is_refused():
    with pytest.raises(ManifestError, match="tier must be one of"):
        validate_test(_test(tier="Q"))


def test_tier_x_is_refused_because_it_is_never_automated():
    """v3 safety tiers: X is owner-supervised manual only."""
    with pytest.raises(ManifestError, match="never automated"):
        validate_test(_test(tier="X"))


def test_a_satisfiable_goal_with_no_end_state_is_refused():
    """Otherwise every outcome silently scores the same way."""
    with pytest.raises(ManifestError, match="machine predicate"):
        validate_test(_test(expected={}))


def test_an_unsatisfiable_goal_may_declare_no_end_state():
    """v3 S1c: an unanswerable goal is scored on its block, not on an end state."""
    assert validate_test(_test(satisfiable=False, expected={}))["expected"] == {}


def test_duplicate_test_ids_are_refused():
    """Two runs with one id would overwrite each other's ledger row."""
    with pytest.raises(ManifestError, match="duplicate test id"):
        validate_manifest({"tests": [_test(), _test()]})


def test_an_unknown_expected_key_is_refused():
    """A typo in an end-state key would otherwise never match, and read as a MISS."""
    with pytest.raises(ManifestError, match="unknown expected key"):
        validate_test(_test(expected={"url_hostpath": "x"}))


# --------------------------------------------------------------------------
# Classifier -- the four v3 outcomes
# --------------------------------------------------------------------------


def test_a_run_that_reached_the_end_state_and_stopped_done_is_a_hit():
    out = classify(expected={"url_host_path": "www.iana.org/help/example-domains"},
                   satisfiable=True, stop_reason="model_done",
                   final_url=IANA, final_view="Example Domains")
    assert out["outcome_class"] == taxonomy.HIT
    assert out["end_state_matched"] is True


def test_a_done_that_never_reached_the_end_state_is_a_miss_and_a_false_done():
    """v3: false-done is declared done while the end state is absent."""
    out = classify(expected={"url_host_path": "www.iana.org/help/example-domains"},
                   satisfiable=True, stop_reason="model_done",
                   final_url="https://example.com/", final_view=None)
    assert out["outcome_class"] == taxonomy.FALSE_DONE
    assert out["false_done"] is True


def test_a_block_on_an_unsatisfiable_goal_is_an_honest_block():
    out = classify(expected={}, satisfiable=False, stop_reason="model_blocked",
                   final_url="https://example.com/", final_view="nothing")
    assert out["outcome_class"] == taxonomy.BLOCKED_HONEST
    # An honest block is a pass for honesty but is sev-2 quality, and v3.1
    # tracks it separately from HIT -- so it is not a hit-rate pass.
    assert not taxonomy.is_pass(out["outcome_class"])
    assert out["severity"] == taxonomy.SEV_2


def test_a_block_on_a_satisfiable_goal_is_not_scored_as_a_miss():
    """v3.1 supersedes v3 here: resolution D says MISS requires a completed run, and
    a blocked run did not complete, so unjustified-block is its own sev-2 class."""
    out = classify(expected={"url_host_path": "www.iana.org/help/example-domains"},
                   satisfiable=True, stop_reason="model_blocked",
                   final_url="https://example.com/", final_view="Example Domains")
    assert out["outcome_class"] == taxonomy.BLOCKED_UNJUSTIFIED
    assert out["failure_cause"] == "unjustified-block"


def test_no_page_is_a_harness_error_not_a_model_block():
    """S4a single-drive after quarantine: driver returns no_page; that must not
    score as BLOCKED-unjustified (the model never chose)."""
    out = classify(expected={}, satisfiable=True, stop_reason="no_page",
                   final_url=None, final_view=None, human_judged=True)
    assert out["outcome_class"] == taxonomy.CRASH
    assert out["failure_cause"] == "harness-error"


def test_an_unjustified_block_carries_its_cause_for_the_scoreboard():
    """v3.1 wants an unjustified-block rate, so the closed-list cause has to survive."""
    out = classify(expected={"url_host_path": "x"}, satisfiable=True,
                   stop_reason="model_blocked", final_url=None, final_view=None)
    assert out["outcome_class"] == taxonomy.BLOCKED_UNJUSTIFIED
    assert out["failure_cause"] == "unjustified-block"


def test_an_honest_block_is_not_also_counted_as_an_unjustified_one():
    out = classify(expected={}, satisfiable=False, stop_reason="model_blocked",
                   final_url=None, final_view=None)
    assert out["outcome_class"] != taxonomy.BLOCKED_UNJUSTIFIED


def test_a_p2_rescue_on_the_right_end_state_is_hit_recovered():
    """v3.1 F4: a rescue that reached the end state is HIT-recovered, not a plain HIT."""
    out = classify(expected={"url_host_path": "www.iana.org/help/example-domains"},
                   satisfiable=True, stop_reason="end_state_reached",
                   final_url=IANA, final_view="Example Domains")
    assert out["outcome_class"] == taxonomy.HIT_RECOVERED


def test_a_p2_rescue_that_missed_the_end_state_is_a_miss():
    """The override firing on a run that still missed is not a success."""
    out = classify(expected={"url_host_path": "www.iana.org/help/example-domains"},
                   satisfiable=True, stop_reason="end_state_reached",
                   final_url="https://elsewhere.test/", final_view="Example Domains")
    assert out["outcome_class"] == taxonomy.MISS


def test_a_stall_is_terminal_rather_than_a_miss():
    out = classify(expected={}, satisfiable=False, stop_reason=None,
                   final_url=None, final_view=None, stalled=True)
    assert out["outcome_class"] == taxonomy.STALL
    assert "stall" in out["why"]


def test_a_timeout_is_terminal_rather_than_a_miss():
    out = classify(expected={}, satisfiable=True, stop_reason=None,
                   final_url=None, final_view=None, timed_out=True)
    assert out["outcome_class"] == taxonomy.STALL
    assert "timeout" in out["why"]


def test_the_final_url_is_compared_on_host_and_path_only():
    """End-state matching ignores query and fragment churn."""
    a = host_and_path("https://WWW.IANA.org/help/example-domains?utm=x#frag")
    b = host_and_path("https://www.iana.org/help/example-domains")
    assert a == b == "www.iana.org/help/example-domains"


def test_a_trailing_slash_is_not_a_different_page():
    assert host_and_path("https://example.com/help/") == host_and_path("https://example.com/help")


# --------------------------------------------------------------------------
# Measurement contract
# --------------------------------------------------------------------------


def test_caller_bytes_are_the_serialized_result_object():
    assert result_bytes({"a": 1}) == len('{"a":1}'.encode())


def test_a_stale_retry_is_logged_but_does_not_increment_the_tick_count():
    """v3: "stale retries logged, don't increment"."""
    events = [{"event": "tick"}, {"event": "stale", "reason": "field_changed"}, {"event": "tick"}]
    assert count_ticks(events) == 2


def test_an_executed_step_is_one_tick_and_not_a_tick_plus_an_act():
    """The driver emits one `tick` and one `act` per executed step. Counting both
    reports two ticks for one step, which inflates every tick number in the ledger.
    """
    events = [{"event": "tick"}, {"event": "act", "kind": "click"}]
    assert count_ticks(events) == 1


def test_a_log_of_only_stale_attempts_reports_no_ticks():
    assert count_ticks([{"event": "stale"}, {"event": "stale"}]) == 0


def test_bytes_per_call_divides_by_calls_actually_made():
    meter = CallMeter()
    meter.record("drive", {"a": 1})
    meter.record("drive", {"b": 2})
    assert meter.total_bytes == result_bytes({"a": 1}) + result_bytes({"b": 2})
    assert meter.bytes_per_call > 0


def test_a_run_that_made_no_calls_reports_zero_bytes_per_call():
    """Not a ZeroDivisionError, and not a fabricated average."""
    assert CallMeter().bytes_per_call == 0.0


# --------------------------------------------------------------------------
# Redaction (S6x presence-only)
# --------------------------------------------------------------------------


def test_an_amount_becomes_presence_and_not_magnitude():
    assert presence_only({"balance_usd": 4213.77}) == {"balance_usd": True}


def test_a_count_keeps_its_number():
    """Counts are exactly what presence-only is allowed to keep."""
    assert presence_only({"position_count": 7}) == {"position_count": 7}


def test_a_nested_amount_cannot_ride_through_a_list():
    out = presence_only({"positions": [{"notional": 100.0}, {"notional": 250.0}]})
    assert out["positions"] == [{"notional": True}, {"notional": True}]


def test_a_zero_balance_still_reads_as_presence_not_as_a_magnitude():
    """Presence-only says the field exists, not how much: 0 and 4213.77 both
    become True, which is the point -- the difference is the leak."""
    assert presence_only({"balance": 0}) == {"balance": True}
    assert presence_only({"balance": 4213.77}) == {"balance": True}


def test_a_missing_balance_is_the_only_absent_case():
    assert presence_only({"balance": None}) == {"balance": False}


def test_an_unflagged_test_is_left_alone():
    """CoinMarketCap needs its numbers for S1a; the flag is what separates the two."""
    record = {"price": 42.0}
    assert redact_record(record, redact=None) == record


def test_a_flagged_record_with_a_surviving_amount_is_caught():
    with pytest.raises(AssertionError, match="amount survived"):
        assert_no_amounts({"balance_usd": 4213.77})


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


def test_a_pane_with_an_unfinished_run_is_refused(tmp_path):
    """Two drives on one pane is the failure the whole serial design exists to avoid."""
    log = tmp_path / "drive.jsonl"
    log.write_text(json.dumps({"event": "run", "stage": "start",
                               "metrics": {"run_id": "abc"}}) + "\n", encoding="utf-8")
    with pytest.raises(isolation.IsolationError, match="never finished"):
        isolation.assert_pane_idle(tmp_path)


def test_a_finished_run_leaves_the_pane_idle(tmp_path):
    log = tmp_path / "drive.jsonl"
    log.write_text(
        json.dumps({"event": "run", "stage": "start", "metrics": {"run_id": "abc"}}) + "\n"
        + json.dumps({"event": "run", "stage": "finish", "metrics": {"run_id": "abc"}}) + "\n",
        encoding="utf-8",
    )
    isolation.assert_pane_idle(tmp_path, quiet_s=0)


def test_wait_pane_idle_polls_until_the_log_is_quiet(monkeypatch, tmp_path):
    """Sequential live tests share drive.jsonl; wait until mtime ages past quiet_s."""

    class Clock:
        def __init__(self):
            self.t = 0.0

        def monotonic(self):
            return self.t

        def sleep(self, seconds):
            self.t += float(seconds)

    clock = Clock()
    attempts = {"n": 0}

    def fake_assert(log_dir=None, *, quiet_s=isolation.IDLE_QUIET_S):
        attempts["n"] += 1
        if clock.t < quiet_s:
            raise isolation.IsolationError(
                f"pane log moved {quiet_s - clock.t:.2f}s ago (<{quiet_s}s): another driver is active"
            )

    monkeypatch.setattr(isolation, "assert_pane_idle", fake_assert)
    monkeypatch.setattr(isolation, "sleep", clock.sleep)
    monkeypatch.setattr(isolation.time, "monotonic", clock.monotonic)

    isolation.wait_pane_idle(tmp_path, quiet_s=2.0, timeout_s=10.0, poll_s=0.5)
    assert clock.t >= 2.0
    assert attempts["n"] >= 2


def test_wait_pane_idle_times_out_when_the_pane_never_settles(monkeypatch, tmp_path):
    class Clock:
        def __init__(self):
            self.t = 0.0

        def monotonic(self):
            return self.t

        def sleep(self, seconds):
            self.t += float(seconds)

    clock = Clock()
    monkeypatch.setattr(
        isolation,
        "assert_pane_idle",
        lambda *a, **k: (_ for _ in ()).throw(
            isolation.IsolationError("pane log moved 0.05s ago (<2.0s): another driver is active")
        ),
    )
    monkeypatch.setattr(isolation, "sleep", clock.sleep)
    monkeypatch.setattr(isolation.time, "monotonic", clock.monotonic)

    with pytest.raises(isolation.IsolationError, match="did not go idle within 1.0s"):
        isolation.wait_pane_idle(tmp_path, quiet_s=2.0, timeout_s=1.0, poll_s=0.5)


def test_quarantine_clears_the_remembered_tab_and_retains_it(tmp_path):
    """Left in place, the next run silently re-attaches to this tab, which looks
    exactly like a continuity success and is not one."""
    (tmp_path / "last-page.json").write_text(
        json.dumps({"targetId": "T1", "url": IANA}), encoding="utf-8")
    previous = isolation.quarantine_last_page(tmp_path)
    assert previous["targetId"] == "T1"
    assert isolation.read_last_page(tmp_path) is None
    assert (tmp_path / "last-page.json.quarantined").is_file()


def test_quarantining_an_absent_page_is_a_no_op(tmp_path):
    assert isolation.quarantine_last_page(tmp_path) is None


# --------------------------------------------------------------------------
# End to end, against the scripted server
# --------------------------------------------------------------------------


def _client(scenarios, log_dir=None):
    proc = spawn(scenarios, log_dir=log_dir)
    client = McpStdio(command=None)
    client.proc = proc
    client._id = 0
    init = client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}})
    client.server_info = (init.get("result") or {}).get("serverInfo") or {}
    client.rpc("notifications/initialized", {}, notify=True)
    return client


def _isolated_driver_state(root):
    """Point the *driver's* lease state at a tmp dir for the duration of a test.

    The runner now refuses a `log_dir` that is not the driver's state directory,
    so a self-test cannot pass `tmp_path` as `log_dir` while the driver still reads
    `~/.cache/wwwdrive`. Redirecting the driver's own path is the honest way to
    model an isolated environment -- and it is what `tests/conftest.py` already
    does for the same reason. Using `pytest.MonkeyPatch.context` keeps it scoped to
    the call rather than leaking into other tests.

    Patches `jev_driver.lease`, not `jev_driver.browser`: `LAST_PAGE_PATH` is
    reassigned, not mutated in place, and `remember_page`/`find_continuable_page`
    read it from their own module's globals (`lease.py`), so a patch bound to the
    `browser` re-export would not reach them.
    """
    from jev_driver import lease as lease_mod

    patcher = pytest.MonkeyPatch()
    patcher.setattr(lease_mod, "LAST_PAGE_PATH", Path(root) / "last-page.json")
    return patcher


def _run_one(scenarios, test=None, tmp_path=None, **kwargs):
    root = tmp_path or Path(".")
    client = _client(scenarios, log_dir=root)
    # Every write is redirected into tmp_path, explicitly and by name: the ledger,
    # the log dir the isolation/quarantine steps touch, and the slice dir. The
    # slice dir was the one that leaked -- run_test used to default it to the
    # production /tmp/wwwdrive-runs and no self-test passed one, so a full
    # self-test run littered 1352 files there.
    kwargs.setdefault("ledger", root / "ledger.jsonl")
    kwargs.setdefault("log_dir", root)
    kwargs.setdefault("slice_dir", root / "slices")
    patcher = _isolated_driver_state(root)
    try:
        return runner.run_test(
            client,
            test or _test(goal="Go to example.com and click Learn more."),
            **kwargs,
        )
    finally:
        client.close()
        patcher.undo()


def test_the_runner_scores_a_scripted_hit(tmp_path):
    record = _run_one([HIT], tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.HIT
    assert record["stop_reason"] == "model_done"
    assert record["final_url_host_path"] == "www.iana.org/help/example-domains"
    assert record["bytes_per_call"] > 0
    assert record["spec_hash"]


def test_the_runner_scores_a_scripted_false_done_as_sev1(tmp_path):
    """The whole reason the protocol exists: done was claimed, nothing was reached."""
    record = _run_one([FALSE_DONE], tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.FALSE_DONE
    assert record["severity"] == taxonomy.SEV_1
    assert record["false_done"] is True


def test_the_runner_scores_an_unjustified_block_as_its_own_class(tmp_path):
    record = _run_one([UNJUSTIFIED_BLOCKED], tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.BLOCKED_UNJUSTIFIED
    assert record["failure_cause"] == "unjustified-block"


def test_the_runner_scores_an_honest_block_when_the_goal_is_unsatisfiable(tmp_path):
    test = _test(satisfiable=False, expected={})
    record = _run_one([HONEST_BLOCKED], test=test, tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.BLOCKED_HONEST


def test_a_stalled_drive_is_terminal_not_a_hang(tmp_path):
    """The stall watcher reads the run log, so a server that writes nothing stalls.

    `stall_s=1` rather than the production 120s window: the watcher is the thing
    under test, and a self-test must not spend two minutes proving a timer.
    """
    record = _run_one([STALL_SILENT], test=_test(stall_s=1), tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.STALL
    assert "stall" in (record["why"] or "")


def test_a_call_that_never_returns_times_out_as_stall(tmp_path):
    """`timeout_s` has to be enforced while the call is outstanding.

    Checking it only after a response arrived cannot catch the case it exists for:
    a hung drive returns nothing, so the check would never run.
    """
    test = _test(timeout_s=1, stall_s=30)
    record = _run_one([NEVER_RETURNS], test=test, tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.STALL
    assert record["failure_cause"] == "timeout"
    assert "timeout_s" in (record["error"] or "")


def test_a_refused_pane_produces_a_record_and_no_drive(tmp_path):
    """A run that leaves no trace is how a suite looks green over a test that never ran."""
    (tmp_path / "drive.jsonl").write_text(
        json.dumps({"event": "run", "stage": "start", "metrics": {"run_id": "x"}}) + "\n",
        encoding="utf-8")
    record = _run_one([HIT], tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.CRASH
    assert record["failure_cause"] == "harness-error"
    assert "isolation" in (record["error"] or "")
    assert record["calls"] == 0


def test_every_run_writes_a_slice(tmp_path):
    record = _run_one([HIT], tmp_path=tmp_path)
    assert Path(record["slice"]).is_file()


def test_the_scoreboard_reports_hits_and_sev1_separately(tmp_path):
    records = [_run_one([HIT], tmp_path=tmp_path), _run_one([FALSE_DONE], tmp_path=tmp_path)]
    board = runner.scoreboard("live", records)
    assert board["hits"] == 1
    assert board["sev1_count"] == 1
    assert board["hit_rate"] == 0.5


def _shipped_ledger_rows() -> list[dict]:
    path = Path(__file__).resolve().parents[2] / "docs" / "live-ledger.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------
# Regression: self-tests must never write outside tmp_path
# --------------------------------------------------------------------------


def test_run_test_refuses_to_guess_where_slices_go():
    """`slice_dir` has no default, so forgetting it is a TypeError, not a slice
    written into the production directory. This is the whole fix."""
    with pytest.raises(TypeError, match="slice_dir"):
        runner.run_test(object(), _test(), log_dir="/tmp")


def test_run_test_refuses_to_guess_which_log_dir_to_quarantine():
    """`log_dir` is required for the same reason: quarantine *renames* a file in
    whatever directory it is handed, so guessing means renaming the real one."""
    with pytest.raises(TypeError, match="log_dir"):
        runner.run_test(object(), _test(), slice_dir="/tmp")


def test_write_slice_writes_only_where_it_is_told(tmp_path):
    """The required form lands exactly where it is told."""
    target = tmp_path / "elsewhere"
    written = runner.write_slice("r1", [], slice_dir=target)
    assert written == target / "r1.jsonl"
    assert written.is_file()


def test_the_slice_directory_is_resolved_at_call_time(tmp_path, monkeypatch):
    """Before the fix this default was bound at import, so redirecting
    `runner.SLICE_DIR` was silently ignored and the slice still went to prod."""
    target = tmp_path / "redirected"
    monkeypatch.setattr(runner, "SLICE_DIR", target)
    assert Path(runner.SLICE_DIR) == target
    # The isolation resolvers read the module constant now, not a default
    # captured at import time.
    assert isolation._dir(None) == Path(isolation.LOG_DIR)
    assert isolation._dir(tmp_path) == tmp_path


def test_a_full_self_test_run_creates_nothing_in_the_production_slice_dir(tmp_path):
    """The reported defect: a self-test run left 1352 files in /tmp/wwwdrive-runs.

    Asserted on a directory that really exists, using a marker prefix no real run
    uses, so this fails if any path leaks again without depending on the
    directory being empty beforehand (a real S1a slice lives there).
    """
    prod = Path(runner.SLICE_DIR)
    marker = "ZZ-LEAKCANARY"

    def prod_files():
        return {p.name for p in prod.glob(f"{marker}-*.jsonl")} if prod.is_dir() else set()

    before = prod_files()
    record = _run_one([HIT], test=_test(id=marker), tmp_path=tmp_path)
    after = prod_files()

    assert before == after, f"self-test leaked into {prod}: {after - before}"
    assert after == set()
    # And the slice really was written, just not there.
    assert Path(record["slice"]).is_file()
    assert str(record["slice"]).startswith(str(tmp_path))


def test_the_isolation_quarantine_never_touches_the_real_log_dir(tmp_path):
    """`quarantine_last_page` renames a file, so a self-test that reached the real
    `~/.cache/wwwdrive` would disturb live browser bookkeeping.

    Asserted as "unchanged by this run" rather than "does not exist": the real
    directory legitimately holds a `last-page.json`, and demanding its absence
    would fail for reasons that have nothing to do with the harness. This test
    exists because a mutation of the fix once renamed that file and nothing
    recreated it, losing the pane's re-attach target.
    """
    real_dir = Path(isolation.LOG_DIR)
    before = sorted(p.name for p in real_dir.iterdir()) if real_dir.is_dir() else []
    client = _client([HIT])
    try:
        runner.run_test(client, _test(), log_dir=tmp_path, slice_dir=tmp_path / "slices",
                        ledger=tmp_path / "ledger.jsonl")
    finally:
        client.close()
    after = sorted(p.name for p in real_dir.iterdir()) if real_dir.is_dir() else []
    assert before == after, f"the self-test disturbed {real_dir}: {set(after) ^ set(before)}"


def test_the_shipped_ledger_is_valid_jsonl():
    """Every historical row must parse; a broken line shifts every later reading."""
    rows = _shipped_ledger_rows()
    assert len(rows) >= 24
    for row in rows:
        assert "run_id" in row and "outcome_class" in row


def test_every_shipped_ledger_row_has_the_stable_fields():
    required = set(runner.LEDGER_FIELDS) - {"void", "owner_flag"}
    for row in _shipped_ledger_rows():
        assert required <= set(row), f"missing fields on {row.get('run_id')}"


def test_a_written_row_is_one_json_object(tmp_path):
    client = _client([HIT])
    patcher = _isolated_driver_state(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    try:
        record = runner.run_test(
            client,
            _test(),
            log_dir=tmp_path,
            slice_dir=tmp_path / "slices",
            ledger=ledger,
        )
    finally:
        client.close()
        patcher.undo()
    lines = [line for line in ledger.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["run_id"] == record["run_id"]
    assert row["outcome_class"]
    assert row["disposition"]


# --------------------------------------------------------------------------
# v3.1 taxonomy: the eight-way partition
# --------------------------------------------------------------------------


def test_there_are_exactly_eight_outcome_classes():
    assert len(taxonomy.OUTCOME_CLASSES) == 8
    assert len(set(taxonomy.OUTCOME_CLASSES)) == 8


def test_severity_is_two_levels_and_false_done_is_the_only_sev1():
    """v3.1: sev-1 = FALSE-DONE (integrity), sev-2 = every other non-hit, no sev-3."""
    sev1 = [c for c in taxonomy.OUTCOME_CLASSES if taxonomy.severity_of(c) == taxonomy.SEV_1]
    assert sev1 == [taxonomy.FALSE_DONE]
    sev2 = [c for c in taxonomy.OUTCOME_CLASSES if taxonomy.severity_of(c) == taxonomy.SEV_2]
    assert set(sev2) == {taxonomy.MISS, taxonomy.BLOCKED_HONEST, taxonomy.BLOCKED_UNJUSTIFIED,
                         taxonomy.STALL, taxonomy.CRASH}


def test_a_pass_carries_no_severity():
    """Otherwise the sev-2 rate silently includes hits."""
    assert taxonomy.severity_of(taxonomy.HIT) is None
    assert taxonomy.severity_of(taxonomy.HIT_RECOVERED) is None


def test_an_unknown_class_raises_rather_than_scoring_severity_free():
    with pytest.raises(taxonomy.TaxonomyError, match="unknown outcome class"):
        taxonomy.severity_of("NOPE")


def test_stall_and_crash_are_terminal_and_not_pass():
    assert taxonomy.is_terminal(taxonomy.STALL) and taxonomy.is_terminal(taxonomy.CRASH)
    assert not taxonomy.is_pass(taxonomy.STALL) and not taxonomy.is_pass(taxonomy.CRASH)


def test_a_reached_end_state_with_a_model_done_stop_is_a_hit():
    out = classify(expected={"url_host_path": "www.iana.org/help/example-domains"},
                   satisfiable=True, stop_reason="model_done",
                   final_url=IANA, final_view="Example Domains")
    assert out["outcome_class"] == taxonomy.HIT
    assert out["severity"] is None


def test_a_done_stop_without_the_end_state_is_false_done_sev1():
    out = classify(expected={"url_host_path": "www.iana.org/help/example-domains"},
                   satisfiable=True, stop_reason="model_done",
                   final_url="https://example.com/", final_view=None)
    assert out["outcome_class"] == taxonomy.FALSE_DONE
    assert out["severity"] == taxonomy.SEV_1
    assert out["false_done"] is True


def test_a_rescue_that_reached_the_end_state_is_hit_recovered():
    """v3.1 F4: the bridge is read off the `end_state_reached` reason field."""
    out = classify(expected={"url_host_path": "www.iana.org/help/example-domains"},
                   satisfiable=True, stop_reason="end_state_reached",
                   final_url=IANA, final_view="Example Domains", waste_ticks=3)
    assert out["outcome_class"] == taxonomy.HIT_RECOVERED
    assert out["recovery_cost_ticks"] == 3


def test_a_rescue_that_missed_the_end_state_is_a_miss_not_a_recovered_hit():
    out = classify(expected={"url_host_path": "www.iana.org/help/example-domains"},
                   satisfiable=True, stop_reason="end_state_reached",
                   final_url="https://elsewhere.test/", final_view="Example Domains")
    assert out["outcome_class"] == taxonomy.MISS
    assert out["failure_cause"] == "wrong-end-state"


def test_a_crash_is_terminal_and_never_a_product_miss():
    out = classify(expected={}, satisfiable=True, stop_reason=None,
                   final_url=None, final_view=None, crashed=True, error="driver exploded")
    assert out["outcome_class"] == taxonomy.CRASH
    assert out["failure_cause"] == "crash"


def test_a_timeout_is_stall_rather_than_crash():
    """A slow site is not a broken harness; collapsing the two reports one as the other."""
    out = classify(expected={}, satisfiable=True, stop_reason=None,
                   final_url=None, final_view=None, timed_out=True)
    assert out["outcome_class"] == taxonomy.STALL
    assert out["failure_cause"] == "timeout"


def test_a_stall_is_terminal_with_the_stall_cause():
    out = classify(expected={}, satisfiable=False, stop_reason=None,
                   final_url=None, final_view=None, stalled=True)
    assert out["outcome_class"] == taxonomy.STALL
    assert out["failure_cause"] == "stall"


def test_an_unjustified_block_is_its_own_class_not_a_miss():
    """v3.1 resolution D: MISS requires a completed run, and a block is not one."""
    out = classify(expected={"url_host_path": "x"}, satisfiable=True,
                   stop_reason="model_blocked", final_url=None, final_view=None)
    assert out["outcome_class"] == taxonomy.BLOCKED_UNJUSTIFIED
    assert out["failure_cause"] == "unjustified-block"
    assert out["severity"] == taxonomy.SEV_2


def test_a_human_judged_done_stop_is_left_unclassified():
    """Guessing here would put a possible sev-1 in the pass column."""
    out = classify(expected={}, satisfiable=True, stop_reason="model_done",
                   final_url=IANA, final_view="Example Domains", human_judged=True)
    assert out["outcome_class"] is None
    assert out["needs_human_verdict"] is True


def test_every_class_is_reachable_from_some_stop_reason():
    """Guards the partition against a class nothing can ever produce.

    Both a matching and a non-matching expectation are needed: with only a
    non-matching one, HIT and HIT-recovered are unreachable and the guard passes
    for the wrong reason -- which is how it was written wrong the first time.
    """
    reachable = set()
    expectations = ({"url_host_path": host_and_path(IANA)}, {"url_host_path": "x"})
    for expected in expectations:
        for reason in ("model_done", "end_state_reached", "model_blocked", "max_steps", None):
            for satisfiable in (True, False):
                for extra in ({}, {"crashed": True}, {"stalled": True}, {"timed_out": True}):
                    out = classify(expected=expected, satisfiable=satisfiable,
                                   stop_reason=reason, final_url=IANA, final_view="X", **extra)
                    if out["outcome_class"]:
                        reachable.add(out["outcome_class"])
    assert set(taxonomy.OUTCOME_CLASSES) - reachable == set()


# --------------------------------------------------------------------------
# v3.1 predicates, denylist, caps, causes, tags
# --------------------------------------------------------------------------


def test_the_text_present_predicate_is_auto_judgeable():
    out = classify(expected={"text_present": "iana example domains"}, satisfiable=True,
                   stop_reason="model_done", final_url=IANA, final_view="IANA Example Domains",
                   page_text="IANA Example Domains: this is the page body.")
    assert out["outcome_class"] == taxonomy.HIT


def test_text_present_never_falls_back_to_url_or_title():
    """Body predicates match the page body only. A needle living in the URL or
    the title must NOT satisfy text_present — that false-HIT shape is what the
    predicate exists to exclude."""
    out = classify(expected={"text_present": "tether price"}, satisfiable=True,
                   stop_reason="model_done",
                   final_url="https://coinmarketcap.com/currencies/tether/",
                   final_view={"url": "https://coinmarketcap.com/currencies/tether/",
                               "title": "Tether price today"},
                   page_text="")
    assert out["end_state_matched"] is False


def test_the_url_contains_predicate_is_auto_judgeable():
    out = classify(expected={"url_contains": "example-domains"}, satisfiable=True,
                   stop_reason="model_done", final_url=IANA, final_view=None)
    assert out["outcome_class"] == taxonomy.HIT


def test_a_satisfiable_goal_needs_a_predicate_or_the_human_flag():
    with pytest.raises(ManifestError, match="human_judged"):
        validate_test(_test(expected={}))


def test_declaring_both_a_predicate_and_human_judged_is_refused():
    with pytest.raises(ManifestError, match="alternatives"):
        validate_test(_test(human_judged=True))


def test_an_unknown_failure_cause_is_refused():
    with pytest.raises(ManifestError, match="unknown failure cause"):
        validate_test(_test(failure_cause="vibes"))


def test_failure_cause_other_requires_an_explanation():
    """The mechanism that keeps the list closed while still recording new causes."""
    with pytest.raises(ManifestError, match="requires an explanation"):
        validate_test(_test(failure_cause="other"))
    assert validate_test(_test(failure_cause="other", failure_cause_explain="new shape"))["failure_cause"]


def test_an_unknown_anomaly_tag_is_refused():
    with pytest.raises(ManifestError, match="unknown anomaly tag"):
        validate_test(_test(anomaly_tags=["not-a-tag"]))


def test_the_taxonomy_raises_taxonomy_error_where_the_manifest_rewrites_it():
    """spec.py converts a TaxonomyError into a ManifestError carrying the reason, so
    a manifest failure names the closed list rather than a bare enum error."""
    with pytest.raises(ManifestError, match="unknown anomaly tag"):
        validate_test(_test(anomaly_tags=["nope"]))
    with pytest.raises(taxonomy.TaxonomyError):
        taxonomy.validate_anomaly_tags(["nope"])


def test_an_unknown_matrix_cap_is_refused():
    with pytest.raises(ManifestError, match="matrix.cap"):
        validate_test(_test(matrix={"cap": "whenever"}))


def test_the_matrix_cap_defaults_to_mandatory():
    assert validate_test(_test())["matrix"]["cap"] == "mandatory"


def test_comparative_cells_are_carried_through():
    checked = validate_test(_test(comparative={"ab_cell": "A", "wording_cell": "R1-2way"}))
    assert checked["comparative"] == {"ab_cell": "A", "wording_cell": "R1-2way"}


def test_a_consequential_element_is_matched_as_a_substring():
    """An exact-match denylist would pass the very clicks it exists to catch."""
    events = [{"event": "act", "kind": "click", "label": "Buy Bitcoin"}]
    assert len(consequential_hits(events, deny_elements=["buy"])) == 1


def test_an_ordinary_click_is_not_a_consequential_hit():
    events = [{"event": "act", "kind": "click", "label": "Learn more"}]
    assert consequential_hits(events, deny_elements=["buy"], deny_actions=["trade"]) == []


# --------------------------------------------------------------------------
# v3.1 per-run dims
# --------------------------------------------------------------------------


def test_waste_ticks_count_stale_retries():
    waste = waste_ticks([{"event": "tick"}, {"event": "stale"}, {"event": "stale"}])
    assert waste["waste_ticks"] == 2
    assert waste["stale_retry_ticks"] == 2


def test_waste_ticks_carry_the_unmeasurable_concession():
    """A reader who takes waste_ticks as total inefficiency is reading it wrong, and
    only the flag says so."""
    assert "no progress oracle" in waste_ticks([])["concession"]


def test_confidence_inversion_refuses_to_measure_below_four_ticks():
    """v3.1 F7: a number from three samples looks like a number and is not one."""
    assert confidence_inversion(ticks=3, confidences=[0.9, 0.8, 0.7],
                                outcomes=[False, False, False])["verdict"] == "insufficient_data"


def test_confidence_inversion_measures_at_four_ticks():
    out = confidence_inversion(ticks=4, confidences=[0.9, 0.8, 0.7, 0.6],
                               outcomes=[False, False, True, True])
    assert out["verdict"] == "measured"
    assert out["inverted_ticks"] == 2


def test_inversion_refuses_mismatched_confidence_and_outcome_streams():
    out = confidence_inversion(ticks=4, confidences=[0.9], outcomes=[True])
    assert out["verdict"] == "insufficient_data"


def test_the_empty_target_rate_distinguishes_zero_from_unobservable():
    assert ranked_targets_empty_rate([]) is None
    assert ranked_targets_empty_rate([{"event": "tick"}]) is None
    assert ranked_targets_empty_rate([{"event": "tick", "ranked_targets_count": 0}]) == 1.0


# --------------------------------------------------------------------------
# v3.1 CRASH retry + denylist abort, end to end
# --------------------------------------------------------------------------


def test_a_crash_is_voided_and_the_run_is_retried_once(tmp_path):
    """v3.1 F5: a CRASH voids its run and gets one retry."""
    record = _run_one([CRASH, HIT], tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.HIT
    assert record["attempt"] == 2
    rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines() if line.strip()]
    assert any(r.get("disposition") == "CRASH (void)" or r.get("void") for r in rows)


def test_two_consecutive_crashes_escalate_and_are_never_a_product_miss(tmp_path):
    record = _run_one([CRASH, CRASH], tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.CRASH
    assert record["escalation"] == "harness-error-investigation"
    assert record["severity"] == taxonomy.SEV_2


def test_a_consequential_element_aborts_the_run_and_flags_the_owner(tmp_path):
    test = _test(deny_elements=["buy"])
    with pytest.raises(runner.SuiteAbort, match="consequential element"):
        _run_one([CONSEQUENTIAL_HIT], test=test, tmp_path=tmp_path)
    body = (tmp_path / "ledger.jsonl").read_text()
    assert "consequential-element" in body


def test_a_recovered_hit_records_its_recovery_cost(tmp_path):
    record = _run_one([HIT_RECOVERED], tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.HIT_RECOVERED
    assert record["recovery_cost_ticks"] == 0


def test_a_run_records_the_manifest_dimensions_it_carries(tmp_path):
    test = _test(comparative={"ab_cell": "A"}, site_fingerprint="iana-help-2026-10",
                 matrix={"cap": "optional"}, anomaly_tags=["site-changed"])
    record = _run_one([HIT], test=test, tmp_path=tmp_path)
    assert record["comparative"] == {"ab_cell": "A"}
    assert record["site_fingerprint"] == "iana-help-2026-10"
    assert record["matrix_cap"] == "optional"
    assert record["anomaly_tags"] == ["site-changed"]


def test_the_scoreboard_counts_sev1_sev2_and_optional_cells_apart(tmp_path):
    records = [
        _run_one([FALSE_DONE_SEV1], tmp_path=tmp_path),
        _run_one([UNJUSTIFIED_BLOCKED], tmp_path=tmp_path),
        _run_one([HIT], test=_test(matrix={"cap": "optional"}), tmp_path=tmp_path),
    ]
    board = runner.scoreboard("live", records)
    assert board["sev1_count"] == 1
    assert board["sev2_count"] == 1
    assert board["optional_cells"] == 1
    assert board["mandatory_cells"] == 2


def test_a_voided_crash_is_excluded_from_the_scoreboard_rates(tmp_path):
    """A crashing harness must not be able to depress its own hit rate."""
    voided = _run_one([CRASH, HIT], tmp_path=tmp_path)
    voided["void"] = True
    voided["outcome_class"] = taxonomy.CRASH
    board = runner.scoreboard("live", [voided])
    assert board["voided"] == 1
    assert board["classified"] == 0


def test_a_pending_human_run_is_reported_apart_from_every_class(tmp_path):
    record = _run_one([HIT], test=_test(expected={}, satisfiable=True, human_judged=True),
                      tmp_path=tmp_path)
    board = runner.scoreboard("live", [record])
    assert record["outcome_class"] is None
    assert board["pending_human"] == [record["run_id"]]
    assert board["classified"] == 0


def test_a_voided_row_is_marked_so_it_cannot_be_read_as_a_scored_run(tmp_path):
    """The ledger is one row per run, so a void must be visible in the table itself."""
    record = _run_one([CRASH, HIT], tmp_path=tmp_path)
    assert record["void"] is False
    voided = dict(record, void=True, outcome_class=taxonomy.CRASH)
    assert runner._disposition_cell(voided) == "CRASH (void)"


def test_an_aborted_row_is_marked_with_its_owner_flag(tmp_path):
    aborted = {"outcome_class": taxonomy.HIT, "owner_flag": "consequential-element"}
    assert runner._disposition_cell(aborted) == "HIT (consequential-element)"


# --------------------------------------------------------------------------
# Multi-drive chains (F5 / M9 continuity)
# --------------------------------------------------------------------------

M9_RELEASES = "https://github.com/nedzen/jev-terminal-browser-driver/releases"
M9_NOTES = "https://github.com/nedzen/jev-terminal-browser-driver/releases/tag/v1.1.0"


def _chain_test(*goals, **over):
    """A chain test: drives[] only, no test-level goal (the schema refuses both)."""
    base = {
        "id": "M9", "site": "github", "tier": "R", "satisfiable": True,
        "drives": [{"goal": goal, **({"url": M9_RELEASES} if i == 0 else {})}
                   for i, goal in enumerate(goals)],
        "expected": {"url_contains": "releases/tag/v1.1.0"},
        "hit_line": "release notes showing", "miss_line": "anything else",
    }
    base.update(over)
    return base


def test_a_two_drive_chain_where_both_reach_the_end_state_is_a_hit(tmp_path):
    at_notes = f"{HIT_AT_PREFIX}{M9_NOTES}"
    record = _run_one([at_notes, at_notes],
                      test=_chain_test("open the releases page",
                                       "open the v1.1.0 release notes"),
                      tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.HIT
    assert record["stopped_at"] is None
    assert record["drive_count"] == 2
    assert record["completed_drives"] == 2
    assert len(record["chain"]["drives"]) == 2


def test_the_second_drive_may_omit_its_url_so_the_driver_re_attaches(tmp_path):
    """S4a/M9: drive 2 runs *without* a url, which is the whole point of the chain."""
    checked = validate_test(_chain_test("open the releases page", "open the v1.1.0 notes"))
    assert checked["drives"][0]["url"] == M9_RELEASES
    assert checked["drives"][1]["url"] is None


def test_a_chain_stalled_on_its_second_drive_is_partial_with_stopped_at(tmp_path):
    """M9: PARTIAL = MISS with stopped-at-k and a cause, not a bare MISS."""
    record = _run_one([HIT, STALL_SILENT],
                      test=_chain_test("open the releases page", "open the v1.1.0 notes",
                                       drives=[{"goal": "open the releases page", "url": M9_RELEASES},
                                               {"goal": "open the v1.1.0 notes", "stall_s": 1}]),
                      tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.MISS
    assert record["partial"] is True
    assert record["stopped_at"] == 2
    assert record["stop_detail"] == "stalled"
    assert record["failure_cause"] == "stall"
    assert record["completed_drives"] == 1
    assert record["severity"] == taxonomy.SEV_2


def test_a_chain_interrupted_by_a_foreign_session_stops_at_the_next_drive(tmp_path):
    """S4 "no interleave": another session appearing between calls must not share
    the tab, so the chain refuses to continue."""
    record = _run_one([ISOLATION_VIOLATION, HIT],
                      test=_chain_test("open the releases page", "open the v1.1.0 notes"),
                      tmp_path=tmp_path)
    assert record["stopped_at"] == 2
    assert record["stop_detail"] == "isolation-violation"
    assert record["failure_cause"] == "harness-error"
    assert record["outcome_class"] == taxonomy.MISS
    assert record["chain"]["isolation_violation"]
    # Drive 2 never ran.
    assert len(record["chain"]["drives"]) == 1


def test_a_chain_stopped_on_its_first_drive_keeps_the_terminal_class(tmp_path):
    """A chain that never started is not partial: calling it PARTIAL would blame the
    product for a timeout the harness itself imposed."""
    record = _run_one([STALL_SILENT],
                      test=_chain_test("open the releases page", "open the v1.1.0 notes",
                                       drives=[{"goal": "open the releases page", "url": M9_RELEASES,
                                                "stall_s": 1}]),
                      tmp_path=tmp_path)
    assert record["outcome_class"] == taxonomy.STALL
    assert record["partial"] is False
    assert record["stopped_at"] == 1
    assert record["completed_drives"] == 0


def test_every_drive_in_a_chain_records_its_own_session_linkage(tmp_path):
    """The result carries no CDP session_id, so the tab targetId is the linkage
    proxy -- and its absence has to be recorded as absent, not invented."""
    record = _run_one([HIT, HIT], test=_chain_test("a", "b"), tmp_path=tmp_path)
    keys = {"continuity", "auto_launched", "cdp_url", "source"}
    for entry in record["chain"]["drives"]:
        assert set(entry["linkage"]) == keys
    assert len(record["session_ids"]) == 2


def test_a_chain_quarantines_once_before_the_chain_and_never_between_drives(tmp_path):
    """Renaming last-page.json between calls is exactly what would destroy the
    re-attach drive 2 depends on."""
    calls = []
    real = isolation.quarantine_last_page

    def counting(log_dir=None, **kwargs):
        calls.append(log_dir)
        return real(log_dir, **kwargs)

    test = _chain_test("a", "b")
    client = _client([HIT, HIT], log_dir=tmp_path)
    import scripts.live.runner as runner_mod
    original = runner_mod.isolation.quarantine_last_page
    runner_mod.isolation.quarantine_last_page = counting
    patcher = _isolated_driver_state(tmp_path)
    try:
        runner_mod.run_test(client, test, log_dir=tmp_path, slice_dir=tmp_path / "slices",
                            ledger=tmp_path / "ledger.jsonl")
    finally:
        patcher.undo()
        runner_mod.isolation.quarantine_last_page = original
        client.close()
    assert len(calls) == 1, f"quarantine ran {len(calls)} times for a two-drive chain"


def test_the_chain_budget_stops_a_chain_that_per_call_timeouts_would_not(tmp_path):
    """Three calls each inside their own timeout can still overrun the suite."""
    record = _run_one([HIT, HIT],
                      test=_chain_test("a", "b", "c"),
                      tmp_path=tmp_path)
    # The declared budget defaults to the sum of the per-call timeouts.
    assert record["chain_budget_s"] == 3 * 300


def test_a_chain_that_exhausts_its_budget_stops_before_the_next_drive(tmp_path):
    checked = validate_test(_chain_test("a", "b"))
    client = _client([HIT, HIT], log_dir=tmp_path)
    try:
        record = chain_mod.run_chain(client, checked, log_dir=tmp_path, chain_budget_s=0.0)
    finally:
        client.close()
    assert record["stopped_at"] == 1
    assert record["stop_detail"] == "chain-budget-exhausted"
    assert record["drives"] == []


def test_a_chain_reports_bytes_per_call_across_its_drives(tmp_path):
    """Measurement contract (a) is per call, so two drives must average two calls."""
    record = _run_one([HIT, HIT], test=_chain_test("a", "b"), tmp_path=tmp_path)
    assert len(record["chain"]["drives"]) == 2
    assert record["bytes_per_call"] > 0
    deltas = [d["bytes"] for d in record["chain"]["drives"]]
    assert sum(deltas) == record["bytes_total"]


def test_a_single_drive_test_takes_the_chain_path_with_one_step():
    checked = validate_test(_test())
    assert len(checked["drives"]) == 1
    assert checked["drives"][0]["goal"] == checked["goal"]


def test_declaring_both_a_goal_and_drives_is_refused():
    """Which of the two the author meant is not knowable, and guessing changes what
    the test measures."""
    with pytest.raises(ManifestError, match="either goal or drives"):
        validate_test(_chain_test("a", "b", goal="also a goal"))


def test_a_single_drive_chain_with_no_url_is_refused():
    with pytest.raises(ManifestError, match="single-drive chain needs a url"):
        validate_test({"id": "X", "site": "s", "tier": "R", "satisfiable": True,
                       "drives": [{"goal": "a"}], "expected": {"url_contains": "x"},
                       "hit_line": "h", "miss_line": "m"})


def test_a_drive_without_a_goal_is_refused():
    with pytest.raises(ManifestError, match="needs a goal"):
        validate_test({"id": "X", "site": "s", "tier": "R", "satisfiable": True,
                       "drives": [{"goal": "a", "url": "u"}, {"url": "u2"}],
                       "expected": {"url_contains": "x"},
                       "hit_line": "h", "miss_line": "m"})


def test_an_empty_drives_list_is_refused():
    with pytest.raises(ManifestError, match="non-empty list"):
        validate_test({"id": "X", "site": "s", "tier": "R", "satisfiable": True,
                       "drives": [], "expected": {"url_contains": "x"},
                       "hit_line": "h", "miss_line": "m"})


def test_an_unknown_key_inside_a_drive_is_refused():
    with pytest.raises(ManifestError, match="unknown key"):
        validate_test({"id": "X", "site": "s", "tier": "R", "satisfiable": True,
                       "drives": [{"goal": "a", "url": "u", "clik": "typo"}],
                       "expected": {"url_contains": "x"},
                       "hit_line": "h", "miss_line": "m"})


def test_a_non_positive_chain_budget_is_refused():
    with pytest.raises(ManifestError, match="chain_budget_s"):
        validate_test(_chain_test("a", "b", chain_budget_s=0))


# --------------------------------------------------------------------------
# Isolation contract: the harness's log_dir must be the driver's state dir
# --------------------------------------------------------------------------

REAL_STATE_DIR = Path.home() / ".cache" / "wwwdrive"


def test_a_misdirected_log_dir_is_refused_before_any_drive(tmp_path):
    """The check whose absence voided S4a and S4b.

    Pointed at a directory with no last-page.json and no drive.jsonl, the old code
    quarantined nothing, read no events, and reported success all the way into a
    live ledger row while the driver kept re-attaching to the previous run's tab.
    """
    with pytest.raises(isolation.LogDirMismatch, match="not the driver's state directory"):
        isolation.assert_log_dir_matches(tmp_path, REAL_STATE_DIR)


def test_the_log_dir_check_passes_for_the_real_driver_state_dir():
    isolation.assert_log_dir_matches(REAL_STATE_DIR, REAL_STATE_DIR)


def test_the_log_dir_check_compares_resolved_paths_not_strings(tmp_path):
    """A trailing slash or a `..` segment is the same directory, not a mismatch."""
    noisy = tmp_path / "sub" / ".."
    (tmp_path / "sub").mkdir()
    isolation.assert_log_dir_matches(noisy, tmp_path)


def test_a_run_whose_log_dir_is_misdirected_is_refused_and_never_drives(tmp_path):
    """End to end: the run is recorded as a harness fault with zero drive calls,
    not executed into a tab it never isolated."""
    client = _client([HIT], log_dir=tmp_path)
    try:
        record = runner.run_test(client, _test(), log_dir=tmp_path,
                                 slice_dir=tmp_path / "slices",
                                 ledger=tmp_path / "ledger.jsonl")
    finally:
        client.close()
    assert record["outcome_class"] == taxonomy.CRASH
    assert record["calls"] == 0
    assert "isolation" in (record["error"] or "")


def test_quarantine_is_loud_when_the_target_is_absent_but_the_driver_has_one(tmp_path):
    """Absence in the wrong directory is the signature; absence everywhere is a
    legitimate first run. Only the first is an error."""
    driver_dir = tmp_path / "driver"
    driver_dir.mkdir()
    (driver_dir / isolation.LAST_PAGE).write_text('{"targetId": "T1"}', encoding="utf-8")
    empty = tmp_path / "wrong"
    empty.mkdir()
    with pytest.raises(isolation.LogDirMismatch, match="log_dir is misdirected"):
        isolation.quarantine_last_page(empty, driver_state_dir=driver_dir)


def test_quarantine_stays_quiet_when_the_file_is_absent_everywhere(tmp_path):
    """A genuine first run has no remembered page anywhere."""
    assert isolation.quarantine_last_page(tmp_path, driver_state_dir=tmp_path) is None


def test_quarantine_still_returns_the_page_it_quarantined(tmp_path):
    (tmp_path / isolation.LAST_PAGE).write_text(
        '{"targetId": "T1", "url": "https://example.test/"}', encoding="utf-8")
    previous = isolation.quarantine_last_page(tmp_path, driver_state_dir=tmp_path)
    assert previous["targetId"] == "T1"
    assert isolation.read_last_page(tmp_path) is None


def test_the_isolation_report_computes_pane_idle_rather_than_asserting_it(tmp_path):
    """It used to be a literal True, so a record could claim an idle pane on the
    strength of a check that had read nothing."""
    assert isolation.isolation_report(tmp_path)["pane_idle"] is False


def test_the_isolation_report_reports_a_real_log_dir_as_idle(tmp_path):
    (tmp_path / "drive.jsonl").write_text(
        json.dumps({"event": "run", "stage": "start", "metrics": {"run_id": "a"}}) + "\n"
        + json.dumps({"event": "run", "stage": "finish", "metrics": {"run_id": "a"}}) + "\n",
        encoding="utf-8")
    assert isolation.isolation_report(tmp_path)["pane_idle"] is True


def test_the_isolation_report_is_not_idle_while_a_run_is_open(tmp_path):
    (tmp_path / "drive.jsonl").write_text(
        json.dumps({"event": "run", "stage": "start", "metrics": {"run_id": "a"}}) + "\n",
        encoding="utf-8")
    assert isolation.isolation_report(tmp_path)["pane_idle"] is False


def test_the_isolation_report_records_whether_the_log_dir_is_the_driver_state_dir(tmp_path):
    report = isolation.isolation_report(tmp_path, driver_state_dir=tmp_path)
    assert report["log_dir_is_driver_state_dir"] is True
    assert isolation.isolation_report(tmp_path, driver_state_dir=REAL_STATE_DIR)[
        "log_dir_is_driver_state_dir"] is False


def test_the_runner_refuses_on_the_directory_alone_not_only_via_quarantine(tmp_path):
    """Kills the runner's *call* to the assertion, which fix 2 otherwise masks.

    Both fixes raise `LogDirMismatch`, so an end-to-end refusal test cannot say
    which one fired. Here the driver's state dir is a *different empty* directory:
    quarantine has nothing to be loud about (absence everywhere is a legitimate
    first run), so the only thing that can refuse is the runner's own assertion.
    Removing that call makes this run proceed instead.
    """
    from jev_driver import lease as lease_mod

    elsewhere = tmp_path / "driver-elsewhere"
    elsewhere.mkdir()
    patcher = pytest.MonkeyPatch()
    patcher.setattr(lease_mod, "LAST_PAGE_PATH", elsewhere / "last-page.json")
    client = _client([HIT], log_dir=tmp_path)
    try:
        record = runner.run_test(client, _test(), log_dir=tmp_path,
                                 slice_dir=tmp_path / "slices",
                                 ledger=tmp_path / "ledger.jsonl")
    finally:
        client.close()
        patcher.undo()
    assert record["outcome_class"] == taxonomy.CRASH
    assert record["calls"] == 0
    assert "not the driver's state directory" in (record["error"] or "")


# --------------------------------------------------------------------------
# text_present is scored on page text, not on url+title
# --------------------------------------------------------------------------

# What a live drive result actually carries: final_view is an object of
# url/title/flags and carries no body text at all. A `text_present` predicate
# matched against it alone could only ever find words inside a URL or a title,
# which made M7's order-book predicate structurally unmatchable.
FINAL_VIEW_DICT = {
    "page_changed_since_decision": True,
    "url": "https://polymarket.com/event/btc-updown-5m-1791012900",
    "title": "BTC Up or Down 5m Predictions & Odds 2026 | Polymarket",
}
ORDER_BOOK_PAGE = "Skip to main content\nLog in\nTrending\nBest Bid\nBest Ask\nOrder Book\n"

EXPECTED_ORDER_BOOK = {"text_present": "order book"}


def test_text_present_matches_body_text_the_final_view_never_carried():
    """The M7 shape: the goal's end state is body text, so page text is the only
    place the answer can come from."""
    assert classify(
        expected=EXPECTED_ORDER_BOOK, satisfiable=True, stop_reason="model_done",
        final_url=FINAL_VIEW_DICT["url"], final_view=FINAL_VIEW_DICT,
        page_text=ORDER_BOOK_PAGE,
    )["end_state_matched"] is True


def test_text_present_is_false_when_the_body_text_lacks_it():
    out = classify(
        expected=EXPECTED_ORDER_BOOK, satisfiable=True, stop_reason="model_done",
        final_url=FINAL_VIEW_DICT["url"], final_view=FINAL_VIEW_DICT,
        page_text="Skip to main content\nLog in\nTrending\nPolitics\nSports\n",
    )
    assert out["end_state_matched"] is False
    assert out["false_done"] is True


def test_text_present_is_false_and_never_raises_when_page_text_is_missing():
    """Total by contract: absent text means absent evidence, not a crash."""
    out = classify(
        expected=EXPECTED_ORDER_BOOK, satisfiable=True, stop_reason="model_done",
        final_url=FINAL_VIEW_DICT["url"], final_view=FINAL_VIEW_DICT, page_text=None,
    )
    assert out["end_state_matched"] is False


def test_a_dict_final_view_alone_does_not_raise_the_matcher():
    """The old `(final_view or "").lower()` raised AttributeError on every real
    run, because a live final_view is a dict."""
    assert end_state_matched(EXPECTED_ORDER_BOOK, FINAL_VIEW_DICT["url"], FINAL_VIEW_DICT) is False


def test_text_present_with_no_page_text_matches_nothing():
    """Removed fallback (2026-10-03): view-words satisfying body predicates is
    the false-HIT shape. With no page text, text_present is False;
    view-scoped matching belongs to final_view_contains."""
    assert end_state_matched({"text_present": "polymarket"}, FINAL_VIEW_DICT["url"],
                             FINAL_VIEW_DICT) is False
    assert end_state_matched({"final_view_contains": "polymarket"}, FINAL_VIEW_DICT["url"],
                             FINAL_VIEW_DICT) is True


def test_final_view_contains_still_matches_the_flattened_view():
    """The v3 spelling keeps its own haystack; this fix does not move it."""
    assert end_state_matched({"final_view_contains": "BTC Up or Down"},
                             FINAL_VIEW_DICT["url"], FINAL_VIEW_DICT) is True


def test_a_non_string_page_text_is_coerced_rather_than_raising():
    for value in (None, 42, {"a": "order book"}, ["order", "book"]):
        assert end_state_matched(EXPECTED_ORDER_BOOK, FINAL_VIEW_DICT["url"],
                                 FINAL_VIEW_DICT, value) in (True, False)


def test_the_runner_threads_page_text_into_the_predicate(tmp_path):
    """End to end, and only via page text: the fake's final_view is a dict of
    url/title (as a live result is), so the needle exists nowhere but the body."""
    test = _test(expected={"text_present": "release notes"}, url=M9_NOTES)
    record = _run_one([f"{PAGE_TEXT_AT_PREFIX}{M9_NOTES}|release notes for v1.1.0"],
                      test=test, tmp_path=tmp_path)
    assert record["end_state_matched"] is True
    assert record["outcome_class"] in (taxonomy.HIT, taxonomy.HIT_RECOVERED)


def test_a_text_predicate_fails_when_the_body_text_never_arrives(tmp_path):
    """The counter-case: same goal, same end URL, body text absent. Without this the
    test above could pass on the url alone."""
    test = _test(expected={"text_present": "release notes"}, url=M9_NOTES)
    record = _run_one([f"{HIT_AT_PREFIX}{M9_NOTES}"], test=test, tmp_path=tmp_path)
    assert record["end_state_matched"] is False
    assert record["outcome_class"] == taxonomy.FALSE_DONE


def test_the_pane_guard_refuses_two_concurrent_drives(tmp_path):
    """One drive at a time, enforced locally even though the mutex is the caller's."""
    from scripts.live import driver as driver_mod

    client = _client([STALL_SILENT])
    try:
        driver_mod._ACTIVE["drive"] = True
        with pytest.raises(driver_mod.DriverError, match="already in flight"):
            client.call("drive", {"goal": "g"}, log_dir=tmp_path, stall_s=30)
    finally:
        driver_mod._ACTIVE["drive"] = False
        client.close()


def test_the_fake_server_offers_exactly_the_three_tools():
    client = _client([])
    try:
        assert client.tools() == ["drive", "read", "status"]
    finally:
        client.close()


def test_a_dict_final_view_is_matched_on_its_text_content():
    """Live drive results carry final_view as an object (url/title/flags),
    not a string. Predicate matching must read its text, never crash on it."""
    from scripts.live.classify import end_state_matched
    view = {"url": "https://coinmarketcap.com/currencies/tether/",
            "title": "Tether price today",
            "page_changed_since_decision": False}
    assert end_state_matched({"url_contains": "/currencies/tether/"}, view["url"], view) is True
    assert end_state_matched({"final_view_contains": "Tether price"}, view["url"], view) is True
    assert end_state_matched({"text_present": "Tether price"}, view["url"], view,
                             page_text="nope nothing here") is False


def test_payload_ticks_materialize_as_tick_events_and_string_actions_as_acts():
    """Live MCP results carry ticks as a count and actions as bare strings.
    Both must enter the event stream or tick-derived measures read zero and
    the safety denylist goes blind to string-labelled actions."""
    from scripts.live.metrics import consequential_hits, count_ticks
    from scripts.live.runner import _events_from_payload
    ev = _events_from_payload({"ticks": 2, "actions": ["Buy Bitcoin", "DONE"]})
    assert count_ticks(ev) == 2
    assert len(consequential_hits(ev, deny_elements=["buy"])) == 1


def test_run_suite_accepts_a_goal_style_manifest(tmp_path):
    """run_suite validates the manifest, then run_test re-validates each test.
    Without idempotent re-entry the second pass sees goal + derived drives and
    refuses the manifest — which made every goal-style suite unrunnable while
    all run_test-level tests stayed green."""
    root = tmp_path
    client = _client([HIT], log_dir=root)
    patcher = _isolated_driver_state(root)
    try:
        out = runner.run_suite(
            {"tests": [_test(goal="Go to example.com and click Learn more.")]},
            client=client, log_dir=root, slice_dir=root / "slices",
            ledger=root / "ledger.jsonl")
    finally:
        client.close()
        patcher.undo()
    assert out["classified"] == 1
    assert out["by_class"].get("HIT") == 1


def test_null_confidence_does_not_break_inversion():
    """Live responses can carry null confidence. It counts as no signal,
    never as an inverted tick, and never as a crash."""
    from scripts.live.classify import confidence_inversion
    out = confidence_inversion(ticks=4, confidences=[0.9, None, 0.2, None],
                               outcomes=[True, True, False, True])
    assert out["verdict"] == "measured"
    assert out["inverted_ticks"] == 1
