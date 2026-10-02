"""fresh(): the read-only target probe, its settle retries, and the reason taxonomy.

The probe's JavaScript is executed under node against a stub DOM, so a hit-test that quietly
stops being a hit-test fails here instead of in a live browser. The Python verdict is exercised
against recorded probe payloads, so the retry counts and the taxonomy are exact.
"""

import json
import shutil
import subprocess

import pytest

from jev_driver import browser as browser_module
from jev_driver.browser import (
    MARKER,
    PROBE_REASONS,
    PROBE_RETRIES,
    Browser,
    StalePage,
    _probe_expression,
    _probe_reason,
    _same_target,
)

KEY = [1.0, "https://x.com/home", 0, 0, 1200, 800, [[4, ""]]]
GUARD = [7, "button", "8933 Likes. Like", None, None, None, None, False, None, None, None, None, "/p/1", "@a · 2m"]


def probe(key=KEY, guard=GUARD, *, attached=True, live=True, in_view=True, hit=True, writable=True):
    """One probe answer, shaped exactly as _probe_expression's JS returns it."""
    return [key, guard, [attached, live, in_view, hit, writable]]


def page(key=KEY, guards=None, marker="marker-1"):
    return {"page_key": key, "guards": guards if guards is not None else {"7": GUARD}, "marker": marker}


def other_control(guard=GUARD):
    return [guard[0], guard[1], "8934 Likes. Liked", *guard[3:]]


class FakeBrowser(Browser):
    """A Browser with no CDP: evaluate() answers a queue, and an extra call is a failure."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.expressions = []
        self.slept = []

    def evaluate(self, expression):
        self.expressions.append(expression)
        if not self.answers:
            raise AssertionError(f"unexpected extra evaluate: {expression[:80]}")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sleep(self, seconds):
        self.slept.append(seconds)


@pytest.fixture(autouse=True)
def no_cdp(monkeypatch):
    """Any full re-snapshot or input dispatch while probing is a bug, not a slow test."""

    def tripwire(*args, **kwargs):
        raise AssertionError("fresh() must not re-snapshot or dispatch input")

    monkeypatch.setattr(browser_module, "browser_operation", tripwire)
    monkeypatch.setattr(Browser, "_read_page", tripwire)
    monkeypatch.setattr(Browser, "observe", tripwire)


def capture_events(monkeypatch):
    events = []
    monkeypatch.setattr(browser_module, "write_event", events.append)
    return events


def test_hit_test_failure_blocks_a_click_single_shot_allowed():
    """The guard still matches; only the centre of the button is covered. Old code clicked it."""
    covered = probe(hit=False)
    assert _same_target(page(), 7, covered) is True

    browser = FakeBrowser([covered] * 4)
    assert browser.fresh(page(), {"kind": "click", "node": 7}) is False
    assert browser._fresh_reason == "not_actionable"


def test_stale_click_never_dispatches_input(monkeypatch):
    events = capture_events(monkeypatch)
    browser = FakeBrowser([probe(hit=False)] * 4)
    action = {"id": "e8", "kind": "click", "node": 7, "label": "8933 Likes. Like"}
    with pytest.raises(StalePage):
        browser.act(action, page())
    assert [event["event"] for event in events] == ["stale"]
    assert events[0]["reason"] == "field_changed"
    assert events[0]["probe_reason"] == "not_actionable"


def test_transient_reparent_succeeds_after_retry():
    """A node detached for two frames is a re-render, not a stale page."""
    browser = FakeBrowser([probe(attached=False, live=False), probe(attached=False, live=False), probe()])
    assert browser.fresh(page(), {"kind": "click", "node": 7}) is True
    assert browser._fresh_reason == "ok"
    assert len(browser.expressions) == 3
    assert browser.slept == [0.1, 0.2]


def test_a_reparented_field_is_typed_into_after_settling():
    """The same tolerance applies to a fill, and the value guard still governs it."""
    typed = [4, "textbox", "Search query", "", None, None, False, ""]
    stored = {"4": typed}
    browser = FakeBrowser([probe(attached=False, live=False), probe(key=KEY, guard=typed)])
    assert browser.fresh(page(guards=stored), {"kind": "fill", "node": 4}) is True
    assert browser.slept == [0.1]


def test_three_settles_then_stale():
    browser = FakeBrowser([probe(hit=False)] * 4)
    assert browser.fresh(page(), {"kind": "click", "node": 7}) is False
    assert browser._fresh_reason == "not_actionable"
    assert len(browser.expressions) == 4  # one probe plus PROBE_RETRIES
    assert browser.slept == list(PROBE_RETRIES) == [0.1, 0.2, 0.3]


def test_retry_repeats_one_read_only_probe():
    """No re-snapshot, no action-list rebuild: every round is the same expression."""
    browser = FakeBrowser([probe(hit=False), probe()])
    assert browser.fresh(page(), {"kind": "click", "node": 7}) is True
    assert browser.expressions == [_probe_expression(7)] * 2
    for expression in browser.expressions:
        assert "__jevFast" in expression
        assert "TreeWalker" not in expression  # snapshot.js only
        assert "omitted_actions" not in expression
        assert "querySelectorAll" not in expression


REASONS = [
    # (expected reason, kind, probe answer)
    ("target_detached", "click", probe(attached=False, live=False)),
    ("target_detached", "fill", probe(attached=False, live=False)),
    ("target_detached", "click", [KEY, None, [False, False, False, False, False]]),
    ("target_changed", "click", probe(key=[2.0, *KEY[1:]])),
    ("target_changed", "click", probe(key=[1.0, "https://x.com/explore", *KEY[2:]])),
    ("target_changed", "click", probe(guard=other_control())),
    ("target_changed", "fill", probe(key=[1.0, "https://x.com/explore", *KEY[2:]])),
    ("target_changed", "click", probe(guard=[99, *GUARD[1:]])),
    ("target_changed", "click", None),  # the document that produced the observation is gone
    ("target_changed", "click", [KEY, GUARD]),  # a probe that reported no flags
    ("target_changed", "click", probe(guard=None)),
    ("not_actionable", "click", probe(hit=False)),
    ("not_actionable", "click", probe(in_view=False, hit=False)),
    ("not_actionable", "click", probe(live=False)),
    ("not_actionable", "select", probe(live=False)),
    ("not_actionable", "fill", probe(live=False, writable=False)),
    ("not_writable", "fill", probe(writable=False)),
    ("ok", "click", probe()),
    ("ok", "select", probe()),
    ("ok", "fill", probe()),
]


CASE_IDS = [f"{case[0]}-{index}" for index, case in enumerate(REASONS)]


@pytest.mark.parametrize(("expected", "kind", "current"), REASONS, ids=CASE_IDS)
def test_reason_for_each_cause(expected, kind, current):
    assert _probe_reason(kind, page(), 7, current) == expected
    assert _probe_reason(kind, page(), 7, current) in PROBE_REASONS


def test_the_taxonomy_is_exactly_five_words():
    assert PROBE_REASONS == ("target_detached", "target_changed", "not_actionable", "not_writable", "ok")


def test_fresh_returns_true_only_for_ok():
    browser = FakeBrowser([probe(), *([probe(hit=False)] * 4)])
    assert browser.fresh(page(), {"kind": "click", "node": 7}) is True
    assert browser.fresh(page(), {"kind": "click", "node": 7}) is False


def test_a_changed_document_outranks_a_detached_node():
    """A route change leaves the old node gone; the page is what changed."""
    answer = probe(key=[2.0, *KEY[1:]], attached=False, live=False)
    assert _probe_reason("click", page(), 7, answer) == "target_changed"


def test_a_covered_control_is_still_the_control_that_was_chosen():
    """State (disabled, covered) is reported as not actionable, not as a different control."""
    answer = probe(guard=other_control(), live=False, hit=False)
    assert _probe_reason("click", page(), 7, answer) == "not_actionable"
    assert _probe_reason("click", page(), 7, probe(guard=other_control())) == "target_changed"


def test_a_fill_survives_a_same_url_document_swap():
    swapped = [2.0, *KEY[1:]]
    assert _probe_reason("fill", page(), 7, probe(key=swapped)) == "ok"
    assert _probe_reason("click", page(), 7, probe(key=swapped)) == "target_changed"


def test_a_fill_follows_its_field_not_the_feed():
    """Ticking counts around the box must not cancel typing."""
    ticked = [4, "textbox", "Search query", "", None, None, False, "12 replies · 3m · 14 replies"]
    assert _probe_reason("fill", page(guards={"4": ticked}), 4, probe(key=KEY, guard=ticked)) == "ok"
    typed = [*ticked[:3], "jev", *ticked[4:]]
    assert _probe_reason("fill", page(guards={"4": ticked}), 4, probe(key=KEY, guard=typed)) == "target_changed"


def test_a_covered_field_is_still_typed_into():
    """A field covered by its own label needs focus, not a pointer: _focus_covered_field does it."""
    browser = FakeBrowser([probe(hit=False)])
    assert browser.fresh(page(), {"kind": "fill", "node": 7}) is True
    assert browser._fresh_reason == "ok"


@pytest.mark.parametrize("kind", ["scroll", "wait", "done"])
def test_non_targeted_actions_keep_the_document_check_only(kind):
    browser = FakeBrowser([[1.0, "https://x.com/home"]])
    assert browser.fresh(page(), {"kind": kind}) is True
    assert len(browser.expressions) == 1
    assert browser.slept == []
    assert "performance.timeOrigin, location.href" in browser.expressions[0]
    assert "__jevFast" not in browser.expressions[0]


def test_a_navigated_page_stops_a_non_targeted_action():
    browser = FakeBrowser([[1.0, "https://x.com/explore"]])
    assert browser.fresh(page(), {"kind": "wait"}) is False
    assert browser._fresh_reason == "target_changed"
    assert len(browser.expressions) == 1
    assert browser.slept == []


def test_the_marker_check_is_untouched():
    seen = FakeBrowser([page()["marker"]])
    assert seen.fresh(page()) is True
    assert seen.expressions == [MARKER]
    assert seen.slept == []
    moved = FakeBrowser(["marker-2"])
    assert moved.fresh(page()) is False
    assert moved._fresh_reason == "target_changed"
    assert len(moved.expressions) == 1
    assert moved.slept == []


def test_stale_record_keeps_its_event_name_and_gains_the_reason(monkeypatch):
    events = capture_events(monkeypatch)
    browser = FakeBrowser([[1.0, "https://x.com/explore"]])
    with pytest.raises(StalePage):
        browser.act({"id": "scroll_down", "kind": "scroll", "delta": 560, "label": "Scroll down"}, page())
    assert events[0]["reason"] == "page_changed"
    assert events[0]["probe_reason"] == "target_changed"
    assert events[0]["event"] == "stale"


def test_a_node_that_was_never_observed_is_detached_without_a_probe():
    browser = FakeBrowser([])
    assert browser.fresh(page(), {"kind": "click", "node": "7"}) is False
    assert browser._fresh_reason == "target_detached"
    assert browser.expressions == []


def test_a_probe_that_cannot_be_evaluated_still_propagates():
    """Only verdicts are retried; a document that vanished mid-evaluate is a real StalePage."""
    browser = FakeBrowser([StalePage("Document changed during evaluation")])
    with pytest.raises(StalePage):
        browser.fresh(page(), {"kind": "click", "node": 7})
    assert browser.slept == []


def test_the_probe_names_only_the_node_it_was_asked_about():
    assert "nodes.get(7)" in _probe_expression(7)
    assert "nodes.get(7)" not in _probe_expression(9)


# --- the probe JS, executed against a stub DOM ---------------------------------------------


HARNESS = """
const fs = require('fs');
const expression = fs.readFileSync(process.argv[2], 'utf8');
const spec = JSON.parse(process.argv[3]);
function makeElement(s) {
  const self = {
    isConnected: s.connected !== false,
    matches: (sel) => sel === ':disabled' && !!s.disabled,
    closest: (sel) => {
      const blocked = (sel.includes('inert') && s.inert) || (sel.includes('aria-disabled') && s.ariaDisabled);
      return blocked ? {} : null;
    },
    checkVisibility: () => s.visible !== false,
    readOnly: !!s.readOnly,
    getAttribute: (name) => (s.attributes || {})[name] !== undefined ? s.attributes[name] : null,
    contains: (other) => other === self,
    getBoundingClientRect: () => s.rect || {x: 0, y: 0, width: 0, height: 0},
  };
  return self;
}
const el = spec.omitNode ? null : makeElement(spec.element || {});
globalThis.innerWidth = 1200;
globalThis.innerHeight = 800;
globalThis.document = {elementFromPoint: () => (spec.covered ? {tagName: 'OVERLAY'} : el)};
globalThis.window = spec.noCache ? {} : {
  __jevFast: {
    nodes: new Map([[spec.node === undefined ? 7 : spec.node, el]]),
    guard: (e) => (e && e.isConnected && e.checkVisibility() ? spec.guard : null),
    pageKey: () => spec.pageKey,
  },
};
process.stdout.write(JSON.stringify(eval(expression)));
"""


def run_probe(tmp_path, spec):
    node = shutil.which("node")
    probe_path = tmp_path / "probe.js"
    probe_path.write_text(_probe_expression(7))
    harness_path = tmp_path / "harness.js"
    harness_path.write_text(HARNESS)
    proc = subprocess.run(
        [node, str(harness_path), str(probe_path), json.dumps(spec)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


node_only = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to execute the probe JS")

LIVE = {"element": {"rect": {"x": 100, "y": 200, "width": 80, "height": 30}}, "guard": GUARD, "pageKey": KEY}


def flags_of(answer):
    assert isinstance(answer, list) and len(answer) == 3, answer
    return answer[2]


@node_only
def test_probe_js_reports_five_boolean_flags(tmp_path):
    answer = run_probe(tmp_path, LIVE)
    assert answer[0] == KEY
    assert answer[1] == GUARD
    assert flags_of(answer) == [True, True, True, True, True]


@node_only
def test_probe_js_hit_test_follows_element_from_point(tmp_path):
    assert flags_of(run_probe(tmp_path, {**LIVE, "covered": True})) == [True, True, True, False, True]


@node_only
def test_probe_js_calls_an_off_screen_centre_not_actionable(tmp_path):
    spec = {**LIVE, "element": {"rect": {"x": 100, "y": 2000, "width": 80, "height": 30}}}
    assert flags_of(run_probe(tmp_path, spec)) == [True, True, False, False, True]
    hidden = {**LIVE, "element": {"rect": {"x": 100, "y": 200, "width": 0, "height": 0}}}
    assert flags_of(run_probe(tmp_path, hidden)) == [True, True, False, False, True]


@node_only
@pytest.mark.parametrize(
    "element",
    [
        {"disabled": True},
        {"ariaDisabled": True},
        {"inert": True},
        {"visible": False},
    ],
    ids=["disabled", "aria-disabled", "inert", "hidden"],
)
def test_probe_js_live_needs_enabled_and_visible(tmp_path, element):
    rect = {"x": 100, "y": 200, "width": 80, "height": 30}
    flags = flags_of(run_probe(tmp_path, {**LIVE, "element": {**element, "rect": rect}}))
    assert flags[0] is True  # still connected: detached is reported separately
    assert flags[1] is False
    assert flags[4] is False


@node_only
@pytest.mark.parametrize(
    "element",
    [{"readOnly": True}, {"attributes": {"aria-readonly": "true"}}],
    ids=["readOnly", "aria-readonly"],
)
def test_probe_js_writable_needs_a_field_that_takes_text(tmp_path, element):
    rect = {"x": 100, "y": 200, "width": 80, "height": 30}
    spec = {**LIVE, "element": {**element, "rect": rect}}
    assert flags_of(run_probe(tmp_path, spec))[4] is False
    assert flags_of(run_probe(tmp_path, {**LIVE, "element": {"rect": rect}}))[4] is True


@node_only
def test_probe_js_reports_a_detached_node_and_a_hidden_guard_separately(tmp_path):
    detached = flags_of(run_probe(tmp_path, {**LIVE, "element": {"connected": False, "rect": {}}}))
    assert detached == [False, False, False, False, False]
    hidden_spec = {**LIVE, "element": {"visible": False, "rect": {"x": 0, "y": 0, "width": 5, "height": 5}}}
    hidden = run_probe(tmp_path, hidden_spec)
    assert hidden[1] is None  # guard() refuses a hidden node
    assert hidden[2][0] is True  # but the probe still knows it is connected


@node_only
def test_probe_js_answers_null_when_the_observed_document_is_gone(tmp_path):
    assert run_probe(tmp_path, {**LIVE, "noCache": True}) is None


def test_probe_js_never_snapshots_the_page():
    """The probe asks one question about one node; it does not rebuild the action list."""
    expression = _probe_expression(7)
    assert "nodes.get(7)" in expression
    for forbidden in ("querySelectorAll", "TreeWalker", "omitted_actions", "identity("):
        assert forbidden not in expression
    # Every flag the Python verdict reads is measured here.
    for required in ("attached", "live", "inView", "hit", "writable", "elementFromPoint", "checkVisibility"):
        assert required in expression