"""P4: the geometry flake — probe telemetry, ordering, label recovery, double-probe.

Four independent defects, one probe failure each. They are kept in one file
because they share a single frozen fact from the run log: 14 `not_actionable`
events against example.com's Learn-more link, on a link whose box and hit-test
were clean on every observation. A link that is boxed, in view, and un-covered
should never have been refused.

Score level. `_clickable` and `_decision_label` are pure; the telemetry runs in
node against the driver's own probe expression, exactly as test_freshness.py
does it. No browser, no live drive, no clicks.
"""

import json
import shutil
import subprocess

import pytest

from jev_driver.browser import (
    PROBE_REASONS,
    Browser,
    _clickable,
    _probe_expression,
    _probe_flags,
    _probe_reason,
    _probe_telemetry,
)

# The frozen 21:30-era probe answer: a clean, clickable link.
GOOD = [True, True, True, True, True]
# snapshot.js's pageKey() is a list, not a string: [timeOrigin, href, scroll…].
KEY = [1234.5, "https://example.test/", 0, 0, 1280, 720]
GUARD = [{"role": "link", "checked": None}]


def answer(flags=GOOD, telemetry=None, page_key=None, guard=None):
    out = [KEY if page_key is None else page_key, GUARD if guard is None else guard, list(flags)]
    if telemetry is not None:
        out.append(telemetry)
    return out


def page():
    """A `page` dict every identity check accepts: same document, same guard.

    `_probe_reason` checks the document and the stored guard before it reads any
    flag, so a fixture whose shapes do not match a real page would report
    `target_changed` for every reason and these tests would pass for the wrong
    reason.
    """
    return {"page_key": KEY, "url": KEY[1], "marker": None, "guards": {"7": GUARD}}


# --------------------------------------------------------------------------
# 1. Telemetry: report which half of `live` went false
# --------------------------------------------------------------------------


def test_telemetry_splits_live_into_its_two_halves():
    """`live` is one bit for two facts. A not_actionable that never reaches the
    hit-test could not say which half went false, which is why the 14 recorded
    events were unattributable."""
    detail = _probe_telemetry(answer(telemetry={"enabled": True, "visible": False, "opacity": "0"}))
    assert detail["enabled"] is True
    assert detail["visible"] is False
    assert detail["opacity"] == "0"


def test_an_answer_without_telemetry_reports_nothing_rather_than_raising():
    """The diagnostic half is optional by construction: a probe answer that
    predates it must be readable, not an IndexError."""
    assert _probe_telemetry(answer()) == {}
    assert _probe_telemetry(None) == {}
    assert _probe_telemetry([KEY, GUARD, list(GOOD), "not a dict"]) == {}


def test_the_verdict_bits_are_unchanged_by_the_telemetry():
    """Telemetry first, measure, no verdict change. The flags the probe has always
    returned must be identical whether or not the diagnostic is present."""
    bare = _probe_flags(answer())
    with_extra = _probe_flags(answer(telemetry={"enabled": True, "visible": True, "opacity": "1"}))
    assert bare == with_extra == (True, True, True, True, True)


def test_the_probe_expression_asks_for_the_halves_it_needs():
    """The field is only useful if the probe actually measures it."""
    expr = _probe_expression(3)
    assert "checkOpacity:true" in expr  # the opacity-sensitive half, which `live` uses
    assert "visiblePlain" in expr or "checkVisibility({})" in expr  # the plain half
    assert "getComputedStyle" in expr  # the number the two halves disagree about
    assert "nodes.get(3)" in expr  # still one node, still read-only


# --------------------------------------------------------------------------
# 2. Ordering: `hit` decides, `live` refuses only what it can attribute
# --------------------------------------------------------------------------


def test_a_visibility_blip_no_longer_refuses_a_clean_hit_test():
    """The flake's shape: boxed, in view, elementFromPoint agrees, and `live`
    false because the opacity-sensitive visibility check disagreed mid-repaint.
    The executor scrollIntoViews and re-hit-tests before dispatching, so it is
    strictly more capable than this probe and is the right gate to defer to."""
    assert _clickable(False, True, {"enabled": True, "boxed": True, "visible": False}) is True


def test_a_disabled_control_is_still_refused():
    """Nothing recovers a disabled control. Refusing is correct and must not be
    traded away for the blip case above."""
    assert _clickable(False, True, {"enabled": False, "boxed": True}) is False


def test_a_control_with_no_box_is_still_refused():
    """No box means no point to aim at, and `hit` would be false anyway. Kept
    explicit because it is the attribution that tells the two apart."""
    assert _clickable(True, False, {"enabled": True, "boxed": False}) is False


def test_a_covered_centre_is_still_refused():
    """`hit` is decisive: an overlay holding the centre is the one case the
    hit-test exists to catch, and deferring it would be the fix going too far."""
    assert _clickable(True, False, {"enabled": True, "boxed": True}) is False


def test_without_telemetry_the_old_rule_stands():
    """A probe answer that predates the diagnostic is judged exactly as it was:
    no telemetry means no attribution, so no deferral."""
    assert _clickable(False, True, {}) is False
    assert _clickable(True, True, {}) is True


def test_a_fill_is_never_deferred_to_the_executor():
    """Typing needs a field, not a pointer, and `_probe_reason` gates fills on
    `live` directly. The deferral is for pointer targets only."""
    assert _probe_reason("fill", page(), 7, answer([True, False, True, True, True])) == "not_actionable"


def test_not_actionable_is_still_reachable_and_still_one_of_the_reasons():
    """The reason it names for a genuinely unclickable control is unchanged."""
    current = answer([True, True, True, False, False], {"enabled": True, "boxed": True})
    assert _probe_reason("click", page(), 7, current) == "not_actionable"
    assert _probe_reason("click", page(), 7, current) in PROBE_REASONS


# The two tests below pin `_probe_reason` on a telemetry-carrying answer rather than
# `_clickable` on its own. `_clickable`'s unit tests pass whether or not the call
# site consults it, so they cannot tell "the deferral is wired up" from "the helper
# is correct and never used" — which is how a revert of the call site passed the
# whole suite (merger spot-check; my M2 claim was wrong about this one).


def test_the_blip_is_deferred_by_the_verdict_and_not_only_by_the_helper():
    """`live` false, but the telemetry says enabled and boxed and the hit-test is
    clean: the driver reports `ok` and lets the executor — which scrollIntoViews and
    re-hit-tests before dispatching — be the gate. This is the recorded flake."""
    current = answer(
        [True, False, True, True, False],
        {"enabled": True, "visible": False, "visiblePlain": True, "opacity": "0", "boxed": True},
    )
    assert _probe_reason("click", page(), 7, current) == "ok"


def test_the_declinable_twins_are_still_not_actionable_through_the_verdict():
    """Same answer shape, same call path, deferral declined: a disabled control and
    a boxless one are refused, and so is a centre somebody else holds. Were the call
    site reading `live and hit` these would all read `not_actionable` — which is
    what makes this the twin that pins the wiring rather than the helper."""
    disabled = answer(
        [True, False, True, True, False],
        {"enabled": False, "visible": False, "visiblePlain": True, "opacity": "0", "boxed": True},
    )
    boxless = answer(
        [True, False, True, False, False],
        {"enabled": True, "visible": False, "visiblePlain": True, "opacity": "0", "boxed": False},
    )
    covered = answer([True, True, True, False, False], {"enabled": True, "visible": True, "boxed": True})
    assert _probe_reason("click", page(), 7, disabled) == "not_actionable"
    assert _probe_reason("click", page(), 7, boxless) == "not_actionable"
    assert _probe_reason("click", page(), 7, covered) == "not_actionable"


def test_a_detached_node_is_still_detached_not_a_deferral():
    """`attached` is checked before any of this and is final."""
    current = answer([False, False, False, False, False], {"enabled": False, "boxed": False})
    assert _probe_reason("click", page(), 7, current) == "target_detached"


# --------------------------------------------------------------------------
# 3. The stale event says what it knows
# --------------------------------------------------------------------------


def test_the_stale_event_reports_which_half_failed():
    """The whole point of the telemetry: `probe_reason: not_actionable` alone
    could not be acted on, and these are the numbers that make it actionable."""
    browser = Browser.__new__(Browser)
    browser._fresh_reason = None
    browser._probe_detail = {}
    browser.session = None
    # The probe says "clean box, clean hit-test, but disabled": the one refusal
    # `live` can attribute, and therefore the one hunk 2 does not defer.
    browser.evaluate = lambda expression: answer(
        [True, False, True, True, False], {"enabled": False, "visible": False, "opacity": "0", "boxed": True}
    )
    recorded = {}

    def capture(event):
        recorded.update(event)

    import jev_driver.browser as module

    original = module.write_event
    module.write_event = capture
    try:
        with pytest.raises(module.StalePage):
            browser.act({"kind": "click", "label": "Learn more", "node": 7}, page())
    finally:
        module.write_event = original

    assert recorded["probe_reason"] == "not_actionable"
    assert recorded["probe"]["visible"] is False
    assert recorded["probe"]["opacity"] == "0"


def test_the_stale_event_omits_the_diagnostic_when_there_was_none():
    """Absent, not an empty object: a reader must be able to tell "not measured"
    from "measured and clean"."""
    browser = Browser.__new__(Browser)
    browser._fresh_reason = None
    browser._probe_detail = {}
    # A detached node, carrying no telemetry: the diagnostic is absent.
    browser.evaluate = lambda expression: answer([False, False, False, False, False])
    recorded = {}

    def capture(event):
        recorded.update(event)

    import jev_driver.browser as module

    original = module.write_event
    module.write_event = capture
    try:
        with pytest.raises(module.StalePage):
            browser.act({"kind": "click", "label": "X", "node": 7}, page())
    finally:
        module.write_event = original

    assert recorded["probe"] is None


# --------------------------------------------------------------------------
# 4. The double probe
# --------------------------------------------------------------------------


class _CountingBrowser(Browser):
    """Counts probe evaluations, so duplicated work is visible.

    The counter is per instance, not per class: a class attribute would carry
    across tests and make the probe counts depend on test order.

    `reason` is the verdict it should reach. For `not_actionable` it reports a
    genuinely disabled control (`enabled: false`), which nothing recovers — not
    the visibility blip, which hunk 2 deliberately stops refusing and which would
    otherwise make every count in this class read as a single probe.
    """

    def __init__(self, reason):
        self.reason = reason
        self._fresh_reason = None
        self._probe_detail = {}
        self.probes = 0

    def evaluate(self, expression):
        self.probes += 1
        refused = self.reason != "ok"
        return answer(
            [True, not refused, True, not refused, True],
            {"enabled": not refused, "visible": True, "boxed": True},
        )

    def sleep(self, seconds):
        return None

    def snapshot(self):
        return {}


def _target():
    return {"kind": "click", "node": 7, "label": "Learn more"}


class _StubMetrics:
    def record_stale(self):
        return None

    def snapshot(self):
        return {}


class _RecordingBrowser:
    """Records whether each act asked for a re-probe, and never raises."""

    def __init__(self):
        self.reprobe_flags = []
        self.acts = []

    def _observe_once(self, screenshot=False):
        return {"actions": [dict(OBSERVED["actions"][0])], "url": "https://example.test/", "text": "t", "scroll": {}}

    def observe(self, screenshot=False):
        return self._observe_once(screenshot)

    def act(self, action, page, text=None, *, reprobe=True):
        self.reprobe_flags.append(reprobe)
        self.acts.append(action)

    def snapshot(self):
        return {}


def test_the_click_retry_actually_asks_for_the_single_probe():
    """The flag existing is not the flag being used. Without this, `_retry_click`
    could go on paying the full settle window and every other test here would
    still pass — the duplicated work is only gone if the caller opts out."""
    from jev_driver.drive_agent import DriveAgent

    browser = _RecordingBrowser()
    agent = DriveAgent.__new__(DriveAgent)
    agent.state = {"goal": "Click Learn more", "page": OBSERVED, "history": [], "browser": browser}
    agent._clicked = []
    agent._would_undo = lambda action, page: False
    agent._time_budget_spent = lambda: False
    agent.screenshots = False
    # `metrics` is a property with no setter; write the attribute it reads.
    agent.__dict__["_metrics"] = _StubMetrics()
    agent._retry_click(WITH_CRITERIA)
    assert browser.reprobe_flags == [False]


def test_a_retrying_caller_still_gets_the_settle_window():
    """The default is unchanged: four probes over 0.6s, because a node mid-repaint
    can still turn into `ok` and the run should wait for that."""
    browser = _CountingBrowser("not_actionable")
    browser._probe_target("click", page(), 7)
    assert browser.probes == 4


def test_a_caller_that_already_re_read_does_not_pay_for_the_window_again():
    """`_retry_click` re-observes on the line above its `act`, so the settle
    window re-decides the node it just decided. One probe, same verdict, no
    duplicated wall clock."""
    browser = _CountingBrowser("not_actionable")
    reason = browser._probe_target("click", page(), 7, reprobe=False)
    assert browser.probes == 1
    assert reason == "not_actionable"


def test_skipping_the_window_does_not_change_the_verdict():
    """The same answer either way: the window is a chance to recover to `ok`, and
    a node that is refused throughout is refused without it."""
    settling = _CountingBrowser("not_actionable")
    single = _CountingBrowser("not_actionable")
    assert settling._probe_target("click", page(), 7) == single._probe_target("click", page(), 7, reprobe=False)


def test_a_node_that_recovers_still_recovers_without_the_window():
    """`reprobe=False` gives up the retry, never the verdict: a first-probe `ok`
    is still `ok`."""
    browser = _CountingBrowser("ok")
    assert browser._probe_target("click", page(), 7, reprobe=False) == "ok"
    assert browser.probes == 1


# --------------------------------------------------------------------------
# 5. The label fix
# --------------------------------------------------------------------------


def _agent():
    from jev_driver.drive_agent import DriveAgent

    agent = DriveAgent.__new__(DriveAgent)
    agent.state = {"goal": "Click Learn more", "page": {"actions": []}, "history": []}
    return agent


NO_CRITERIA = {"operation": "CLICK", "target": "1", "request": {"questions": {}}}
WITH_CRITERIA = {
    "operation": "CLICK",
    "target": "1",
    "request": {"questions": {"click_target": {"criteria": {"1": "[1] Learn more; link"}}}},
}
OBSERVED = {"actions": [{"kind": "click", "node": 4, "label": "Learn more", "role": "link"}]}


def test_criteria_still_win_when_they_carry_an_entry():
    """The request's own criteria are the primary source and are unchanged."""
    assert _agent()._decision_label(WITH_CRITERIA, OBSERVED) == "Learn more"


def test_a_missing_criteria_entry_falls_back_to_the_observed_page():
    """The `label: "1"` in the log: criteria carried no entry, so the retry path
    had nothing to click by and fell through to the target key."""
    assert _agent()._decision_label(NO_CRITERIA, OBSERVED) == "Learn more"


def test_no_label_anywhere_is_an_empty_string_not_the_target_key():
    """A caller must be able to decline. Handing back `"1"` makes "I cannot name
    this control" indistinguishable from "this control is called 1"."""
    assert _agent()._decision_label(NO_CRITERIA, {"actions": []}) == ""
    assert _agent()._decision_label(NO_CRITERIA, None) == ""


def test_the_fallback_is_not_used_for_a_fill_retry():
    """That path spends a paid text-helper call on the label it recovers, so
    widening what counts as recoverable there would buy new calls, not new clicks.
    This is the scope boundary, and it is load-bearing."""
    from jev_driver.drive_agent import DriveAgent

    agent = DriveAgent.__new__(DriveAgent)
    agent.state = {
        "goal": "Type husqvarna",
        "page": OBSERVED,
        "history": [],
        "browser": None,
    }
    assert agent._retry_fill(NO_CRITERIA) is None


def test_the_stale_log_line_writes_the_label_and_the_target_separately():
    """The `label: "1"` line in the run log. It read as a control called "1", so
    the target key now travels in its own field and the label is the label or
    nothing. Asserted against the source because the line is written inline in a
    branch that needs a live StalePage to reach."""
    from pathlib import Path

    import jev_driver.drive_agent as module

    source = Path(module.__file__).read_text()
    assert '"label": self._decision_label(decision, self.state.get("page")) or None,' in source
    # The bare target key must not be reachable as a label anywhere.
    assert "or decision.get(\"target\")," not in source
    assert '"target": decision.get("target"),' in source


def test_the_fill_retry_reads_criteria_without_the_observed_page():
    """The scope boundary, pinned at the call: `_retry_fill` asks for the label
    with no page, so the observed-page fallback cannot reach it."""
    from pathlib import Path

    import jev_driver.drive_agent as module

    source = Path(module.__file__).read_text()
    body = source.split("def _retry_fill")[1].split("def _retry_click")[0]
    assert "self._decision_label(decision)" in body
    assert 'self._decision_label(decision, state.get("page"))' not in body


# --------------------------------------------------------------------------
# The probe JS itself, run for real in node
# --------------------------------------------------------------------------


node_only = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to execute the probe JS")


def _harness(tmp_path, spec):
    node = shutil.which("node")
    probe = tmp_path / "probe.js"
    probe.write_text(_probe_expression(7))
    run = tmp_path / "run.js"
    run.write_text(
        "const fs=require('fs');"
        "global.window=global;global.document={};global.innerWidth=1280;global.innerHeight=720;"
        "const spec=JSON.parse(process.argv[2]);"
        "const c={pageKey:()=>%s,guard:()=>%s,nodes:new Map()};"
        "const e=spec.element;"
        "e.isConnected=true;"
        "e.checkVisibility=o=>(o&&o.checkOpacity)?spec.visibleStrict:spec.visible;"
        "e.matches=()=>!!spec.disabled;"
        "e.closest=()=>null;e.readOnly=!!spec.readOnly;e.getAttribute=()=>spec.readonly||null;"
        "e.getBoundingClientRect=()=>({x:100,y:200,width:80,height:30});"
        "e.contains=()=>true;e.tagName='A';"
        "window.__jevFast=c;c.nodes.set(7,e);"
        "document.elementFromPoint=()=>e;"
        "const out=eval(fs.readFileSync(process.argv[3],'utf8'));"
        "process.stdout.write(JSON.stringify(out));"
        % (json.dumps(KEY), json.dumps(GUARD))
    )
    proc = subprocess.run([node, str(run), json.dumps(spec), str(probe)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@node_only
def test_the_probe_reports_its_visibility_halves_in_a_real_js_engine(tmp_path):
    """Runs the driver's own expression, so the telemetry is verified as JS and
    not merely as a Python string that happens to contain the right words.

    The element is plainly visible but its opacity-sensitive check disagrees:
    exactly the disagreement behind the recorded flake, and exactly the case
    where `live` goes false while the box and the hit-test stay clean.
    """
    spec = {"element": {"label": "Learn more"}, "visible": True, "visibleStrict": False}
    got = _harness(tmp_path, spec)
    # live false (the blip), in view, hit true (the box and hit-test are clean).
    # `writable` follows `live`, which is why it is false here and why a click is
    # judged on the telemetry rather than on it.
    assert got[2] == [True, False, True, True, False]
    assert got[3]["enabled"] is True
    assert got[3]["visible"] is False  # the half that went false
    assert got[3]["visiblePlain"] is True  # the half that did not
    assert got[3]["boxed"] is True


@node_only
def test_the_probe_reports_no_opacity_where_the_engine_cannot(tmp_path):
    """`getComputedStyle` is absent in the bare harness. The field must degrade to
    null rather than take the whole probe down — a probe that raises tells us
    nothing about the target."""
    spec = {"element": {"label": "Learn more"}, "visible": True, "visibleStrict": True}
    got = _harness(tmp_path, spec)
    assert got[2] == [True, True, True, True, True]
    assert got[3]["opacity"] is None