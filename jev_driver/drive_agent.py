"""Driver-side Agent subclass. agent.py stays verbatim."""

import re
import time

from .agent import Agent
from .browser import StalePage, without_counts
from .metrics import Metrics, instrument_browser
from .model import action_space, field_context, field_text
from .readiness import (
    REASON_WHY,
    degenerate,
    done_acceptable,
    done_probability,
    end_state_reached,
    page_is_shell,
)
from .runlog import write_event

TIME_BUDGET_WHY = (
    "Stopped: the run's time budget ran out before the goal was visibly done. "
    "The decision that crossed the deadline was discarded, so nothing was clicked or typed after it."
)


def _label_stem(label: str) -> str:
    """Drop a leading count so '12 comments' still matches '13 comments'."""
    parts = label.split(None, 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1].strip()
    return label.strip()


def _observed_label(page: dict | None, target) -> str:
    """The observed page's own label for a target index, or "" when it has none.

    The fallback `_decision_label` reaches for when the request's criteria carry
    no entry for the chosen target. The index the model was offered is assigned by
    `action_space`, so the page's own name for it comes from the same call — this
    is the control's label, not a guess at one.
    """
    if not isinstance(target, str) or not target:
        return ""
    elements = action_space((page or {}).get("actions") or [])[0]
    for element in elements:
        if isinstance(element, dict) and str(element.get("index")) == target:
            label = str(element.get("label") or "").strip()
            if label:
                return label.split(";")[0].strip()
    return ""


def _click_named(actions, label: str):
    """Find the click target again. Exact label, or the same stem when the count changed."""
    exact = []
    stemmed = []
    want = _label_stem(label)
    for item in actions:
        if item.get("kind") != "click":
            continue
        got = (item.get("label") or "").split(" → ")[0].strip()
        if got == label:
            exact.append(item)
        elif want != label and _label_stem(got) == want:
            stemmed.append(item)
    if exact:
        return exact[0]
    if len(stemmed) == 1:
        return stemmed[0]
    return None


def label_of(text, fallback, width=60) -> str:
    """A criterion's readable label: drop the "[KEY]" prefix and the ";" tail, clipped to width."""
    raw = str(text or fallback or "")
    if raw.startswith("[") and "]" in raw:
        raw = raw.split("]", 1)[1]
    return raw.split(";")[0].strip()[:width]


def _why(status, decision, history, reason=None):
    if reason in REASON_WHY:
        return REASON_WHY[reason]
    decision = decision or {}
    if status == "done":
        return "Model chose DONE. Confirm the page; DONE is not proof the goal happened."
    if status == "blocked" and (decision.get("choice") == "BLOCKED" or decision.get("operation") == "BLOCKED"):
        return REASON_WHY["model_blocked"]
    last3 = (history or [])[-3:]
    if (
        status == "blocked"
        and len(last3) == 3
        and all(item.get("page_changed") is False and item.get("kind") != "wait" for item in last3)
    ):
        return "Stopped: three actions in a row left the page unchanged."
    if status == "blocked":
        return "Stopped before the goal was visibly done."
    return ""


_PAGINATION = re.compile(r"^(next|previous|prev|more|load more|show more|continue|older|newer)\b|[›»→←‹«]|^\d+$")
_REPEAT_GOAL = re.compile(r"\b(twice|times|each|every|until|pages|all of)\b")


def _top_operation(decision) -> str | None:
    probs = (decision or {}).get("operation_probabilities") or {}
    if not probs:
        return None
    return max(probs.items(), key=lambda kv: float(kv[1] or 0))[0]


_TERMINAL = {"DONE", "BLOCKED"}


def _performs_input(decision) -> bool:
    """False for DONE and BLOCKED: they only settle the run, they never click or type.

    `choice` is the field the base loop branches on to decide between settling the run
    and touching the page, so it alone decides this. `operation` is deliberately not
    consulted: a decision whose two fields disagree must be read as input, because that
    is the reading that cannot type or click after the deadline.

    The deadline's promise is that nothing is typed or clicked after it, so a decision
    that performs no input is still worth honouring once the clock is spent.
    """
    return (decision or {}).get("choice") not in _TERMINAL


def _ranked(probs, limit=6):
    items = sorted((probs or {}).items(), key=lambda kv: -float(kv[1] or 0))
    return [[str(key), round(float(val), 3)] for key, val in items[:limit]]


class DriveAgent(Agent):
    """Exempt advancing scrolls from the 3-repeat guard; optional debug HUD."""

    def __init__(self, url, goals, *, debug=False, time_budget_s=None, **kwargs):
        started = time.perf_counter()
        super().__init__(url, goals, **kwargs)
        self._metrics = Metrics()
        self._metrics.record_startup((time.perf_counter() - started) * 1000)
        self.debug = debug
        # Optional inner deadline (upstream PR #3 idea). timeout_s stays the
        # outer subprocess kill; this one is checked inside the tick loop.
        self.time_budget_s = None if time_budget_s in (None, "") else int(time_budget_s)
        self._budget_deadline = None
        self._budget_stopped = False
        self._unexecuted_key = None
        self._unexecuted_count = 0
        self._weak_done = 0
        self._degenerate_streak = 0
        self._clicked = []
        self._start_url = (self.state.get("page") or {}).get("url")
        browser = self.state.get("browser")
        if browser is not None:
            browser.debug = debug
            instrument_browser(browser, self._metrics)
            if debug:
                browser.paint_hud(self._hud_payload())

    @property
    def metrics(self) -> Metrics:
        """This run's counters. Created on demand, so a bare instance still works."""
        if self.__dict__.get("_metrics") is None:
            self.__dict__["_metrics"] = Metrics()
        return self.__dict__["_metrics"]

    def close(self) -> None:
        """Close the browser, then write this run's counters. Metrics never break cleanup."""
        started = time.perf_counter()
        try:
            super().close()
        finally:
            self._write_metrics((time.perf_counter() - started) * 1000)

    def _write_metrics(self, cleanup_ms: float) -> None:
        """Freeze and write once per run, next to the run log. Never raises."""
        if getattr(self, "_metrics_written", False):
            return
        self._metrics_written = True
        try:
            state = self.state or {}
            metrics = self.metrics
            metrics.record_cleanup(cleanup_ms)
            metrics.finish(state.get("status"), getattr(self, "_metrics_error", None))
            metrics.write()
        except Exception:
            return

    def _start_time_budget(self) -> None:
        """Start the clock at the first decision, so opening the tab spends none of it."""
        if getattr(self, "time_budget_s", None) and getattr(self, "_budget_deadline", None) is None:
            self._budget_deadline = time.perf_counter() + self.time_budget_s

    def _time_budget_spent(self) -> bool:
        """True once the inner budget is used up. No budget set means never."""
        deadline = getattr(self, "_budget_deadline", None)
        return deadline is not None and time.perf_counter() >= deadline

    def _stop_time_budget(self):
        """Stop on the inner deadline. The pending decision is discarded, never executed.

        One tick reaches this twice: predict refuses the model call, then the tick's
        unconditional act finds the same spent clock. The second visit re-asserts the
        state but must not log again, so a consumer sees exactly one time_budget event.
        """
        state = self.state
        state["decision"] = None
        state["status"] = "blocked"
        state["stop_reason"] = "time_budget"
        if state.get("started_at") is not None:
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        if not self._budget_stopped:
            self._budget_stopped = True
            write_event(
                {
                    "event": "blocked",
                    "goal": state.get("goal"),
                    "reason": "time_budget",
                    "why": TIME_BUDGET_WHY,
                }
            )
        self._paint_hud()
        return self.snapshot()

    def command(self, name, body=None):
        if name == "predict":
            if self._time_budget_spent():
                # Deadline already gone: stop without spending a model call.
                return self._stop_time_budget()
            self._start_time_budget()
            snap = super().command(name, body)
            self.metrics.record_jev((self.state.get("decision") or {}).get("latency_ms"))
            self._paint_hud()
            return snap
        if name == "act":
            if self._time_budget_spent() and _performs_input(self.state.get("decision")):
                # The model call outlived the deadline: drop it unexecuted so the
                # page is never mutated by a decision nobody waited for. DONE and
                # BLOCKED are exempt; they type and click nothing, so a spent clock
                # must not turn a free finish into a failure.
                return self._stop_time_budget()
            rejected = self._stop_low_confidence() or self._reject_weak_done() or self._look_further()
            if rejected is not None:
                self._paint_hud()
                return rejected
            before_y = ((self.state.get("page") or {}).get("scroll") or {}).get("y")
            page = self.state.get("page") or {}
            decision = self.state.get("decision") or {}
            chosen = next((a for a in page.get("actions") or [] if a.get("id") == decision.get("choice")), None)
            if chosen is not None and self._would_undo(chosen, page):
                self.state["decision"] = None
                self._paint_hud()
                return self.snapshot()
            steps_before = len(self.state.get("history") or [])
            typed_before = len(self.state.get("text_calls") or [])
            try:
                snap = super().command("act", body)
            except StalePage as exc:
                self._note_text_helper(typed_before)
                self.metrics.record_stale()
                decision = (self.state.get("decisions") or [None])[-1] or {}
                write_event(
                    {
                        "event": "stale",
                        "goal": self.state.get("goal"),
                        "kind": decision.get("operation"),
                        # The label, or nothing. A bare target key here used to read
                        # as `label: "1"`, which looks like a control called "1" and
                        # hides the fact that the label was never recovered.
                        "label": self._decision_label(decision, self.state.get("page")) or None,
                        "target": decision.get("target"),
                        "why": str(exc),
                    }
                )
                recovered = self._retry_fill(decision) or self._retry_click(decision)
                if recovered is not None:
                    self._unexecuted_key = None
                    self._unexecuted_count = 0
                    self._paint_hud()
                    return recovered
                if self._note_unexecuted(exc):
                    self._paint_hud()
                    return self.snapshot()
                self._metrics_error = exc  # the run dies here; record why, not what it said
                raise
            self._note_text_helper(typed_before)
            if chosen is not None and len(self.state.get("history") or []) > steps_before:
                self._remember_click(chosen, page, (self.state.get("page") or {}).get("url"))
            self._unexecuted_key = None
            self._unexecuted_count = 0
            self._note_model_blocked()
            snap = self._maybe_unblock_scroll(snap, before_y)
            self._paint_hud()
            return snap
        snap = super().command(name, body)
        if name in {"predict", "tick"}:
            self._paint_hud()
        return snap

    def _note_model_blocked(self):
        state = self.state
        decision = (state.get("decisions") or [None])[-1] or {}
        # Logged before the guards, and on every tick the model chose BLOCKED, so
        # the record survives `_blocked_rescue` converting the run to done. A
        # BLOCKED that was rescued is exactly the case worth being able to audit.
        if decision.get("choice") == "BLOCKED" or decision.get("operation") == "BLOCKED":
            self._log_model_blocked(decision)
        if state.get("status") != "blocked" or state.get("stop_reason"):
            return
        if decision.get("choice") == "BLOCKED" or decision.get("operation") == "BLOCKED":
            state["stop_reason"] = "model_blocked"

    def _log_model_blocked(self, decision: dict) -> None:
        """The four facts that decide whether a BLOCKED was earned: what DONE was
        worth on the same page, how many targets were even on offer, where the run
        ended up, and whether this run had navigated to get here.

        `done_p` is the number that was compared against DONE_MIN, and
        `moved_on` is the precondition of the `_reject_weak_done` bypass, so both
        are what a reader needs to say whether a BLOCKED was the model's honest
        read or the framing losing a coin-flip.
        """
        state = self.state
        page = state.get("page") or {}
        write_event(
            {
                "event": "model_blocked",
                "goal": state.get("goal"),
                "reason": "model_blocked",
                "done_p": round(done_probability(decision), 4),
                "ranked_targets_count": len(decision.get("target_probabilities") or {}),
                "final_url": page.get("url"),
                "moved_on": self._moved_on(page),
                "page_text": (page.get("text") or "")[:150],
            }
        )

    def _note_text_helper(self, before: int) -> None:
        """Count helper calls by how many the loop appended, so a cached value costs nothing."""
        for row in (self.state.get("text_calls") or [])[before:]:
            if isinstance(row, dict):
                self.metrics.record_text_helper(row.get("latency_ms"))

    def _decision_label(self, decision: dict, page: dict | None = None) -> str:
        """The readable label for the target this decision chose, or "" when unknowable.

        Reads the request's own criteria first, which is the only source that names a
        target the page no longer shows. When the criteria carry no entry for this
        target it falls back to the observed page's own label for the same index — the
        same fallback the HUD uses (_hud_payload) — because a retry that cannot name the
        control it is retrying clicks by target key instead, which is a different
        control or nothing at all.

        Returns "" rather than the bare target key when neither source has a label.
        Callers use "" to decline the retry; a caller that logs must not be handed a
        target id and print it as if it were a label.
        """
        operation = (decision.get("operation") or "").lower()
        questions = ((decision.get("request") or {}).get("questions") or {})
        criteria = (questions.get(operation + "_target") or {}).get("criteria") or {}
        raw = str(criteria.get(decision.get("target")) or "")
        if raw:
            if raw.startswith("[") and "]" in raw:
                raw = raw.split("]", 1)[1]
            return raw.split(";")[0].strip()
        return _observed_label(page, decision.get("target"))

    def _retry_fill(self, decision: dict):
        """The feed changed during the decision. Type into the same label on a fresh read."""
        if (decision or {}).get("operation") != "TYPE_TEXT":
            return None
        state = self.state
        # Criteria only, no observed-page fallback: this path spends a text-helper
        # call on the label it recovers, so widening what counts as recoverable
        # here would buy new paid calls, not new clicks. The fallback is for the
        # paths that only need a name to log or to match against.
        label = self._decision_label(decision)
        if not label:
            return None
        browser = state.get("browser")
        if browser is None:
            return None
        page = state.get("page") or {}
        safe_page = {"title": page.get("title") or "", "text": page.get("text") or ""}
        try:
            text, helper = field_text(
                field_context(
                    state.get("goal") or "",
                    {"label": label, "role": "", "value": ""},
                    safe_page,
                    state.get("history") or [],
                )
            )
        except (RuntimeError, ValueError) as exc:
            write_event({"event": "retry_fill", "goal": state.get("goal"), "label": label, "why": str(exc)})
            return None
        self.metrics.record_text_helper(helper.get("latency_ms") if isinstance(helper, dict) else None)
        action = None
        for _ in range(2):
            page = browser._observe_once(screenshot=False)
            action = next(
                (
                    item
                    for item in (page.get("actions") or [])
                    if item.get("kind") == "fill" and (item.get("label") or "").split(" → ")[0].strip() == label
                ),
                None,
            )
            if action is None:
                return None
            if self._time_budget_spent():
                # The re-read cost the remaining budget. Discard the retry rather
                # than typing into a field past the deadline.
                return self._stop_time_budget()
            try:
                browser.act(action, page, text=text)
                break
            except StalePage:
                self.metrics.record_stale()
                action = None
        if action is None:
            return None
        state["page"] = browser.observe(screenshot=getattr(self, "screenshots", False))
        history = state.setdefault("history", [])
        history.append(
            {
                "step": len(history) + 1,
                "action": label,
                "kind": "fill",
                "operation": "TYPE_TEXT",
                "text": text,
                "text_helper": helper.get("model") if isinstance(helper, dict) else None,
                "page_changed": True,
                "usage": {},
            }
        )
        extra = helper if isinstance(helper, dict) else {}
        state.setdefault("text_calls", []).append({"field": label, "value": text, **extra})
        state["decision"] = None
        state["status"] = "ready"
        write_event(
            {
                "event": "retry_fill",
                "goal": state.get("goal"),
                "label": label,
                "typed": text[:80],
                "why": "Typed into the same field after the page changed.",
            }
        )
        return self.snapshot()

    def _retry_click(self, decision: dict):
        """The page changed during the decision. Click the same label on a fresh read."""
        if (decision or {}).get("operation") != "CLICK":
            return None
        state = self.state
        label = self._decision_label(decision, state.get("page"))
        if not label:
            return None
        browser = state.get("browser")
        if browser is None:
            return None
        action = None
        for _ in range(2):
            page = browser._observe_once(screenshot=False)
            action = _click_named(page.get("actions") or [], label)
            if action is None:
                return None
            if self._would_undo(action, page):
                return self.snapshot()
            if self._time_budget_spent():
                # Same as the fill path: no click lands after the deadline.
                return self._stop_time_budget()
            try:
                # The page was re-read on the line above, so the probe's settle
                # window has nothing left to wait for: it would re-decide the
                # node we just decided, 0.6s later, and reach the same answer.
                browser.act(action, page, reprobe=False)
                break
            except StalePage:
                self.metrics.record_stale()
                action = None
        if action is None:
            write_event(
                {
                    "event": "blocked",
                    "goal": state.get("goal"),
                    "reason": "click_not_sent",
                    "label": label,
                    "why": REASON_WHY["click_not_sent"],
                }
            )
            return None
        state["page"] = browser.observe(screenshot=getattr(self, "screenshots", False))
        self._remember_click(action, page, state["page"].get("url"))
        history = state.setdefault("history", [])
        history.append(
            {
                "step": len(history) + 1,
                "action": (action.get("label") or label).split(" → ")[0].strip(),
                "kind": "click",
                "operation": "CLICK",
                "page_changed": True,
                "usage": {},
            }
        )
        state["decision"] = None
        state["status"] = "ready"
        write_event(
            {
                "event": "retry_click",
                "goal": state.get("goal"),
                "label": label,
                "why": "Clicked the same control after the page changed.",
            }
        )
        return self.snapshot()

    @staticmethod
    def _control_state(action: dict, page: dict) -> dict:
        rect = action.get("rect") or {}
        scroll = page.get("scroll") or {}
        return {
            "node": action.get("node"),
            "url": page.get("url"),
            "x": float(rect.get("x") or 0) + float(scroll.get("x") or 0),
            "y": float(rect.get("y") or 0) + float(scroll.get("y") or 0),
            "label": action.get("label"),
            "shape": without_counts(action.get("label")),
            "flags": tuple(action.get(key) for key in ("checked", "selected")),
        }

    def _remember_click(self, action: dict, page: dict, after_url: str | None = None) -> None:
        if action.get("kind") == "click":
            record = {**self._control_state(action, page), "led_to": after_url}
            self._clicked = [*getattr(self, "_clicked", []), record]

    def _repeats_ok(self, control: dict) -> bool:
        """Pagination and goals that ask for repeats may follow the same label again."""
        label = str(control.get("label") or "").strip().lower()
        goal = str(self.state.get("goal") or "").lower()
        return bool(_PAGINATION.search(label) or _REPEAT_GOAL.search(goal))

    def _would_undo(self, action: dict, page: dict) -> bool:
        """Stop instead of undoing a toggle or following the same link a second time."""
        if action.get("kind") != "click":
            return False
        now = self._control_state(action, page)
        for before in getattr(self, "_clicked", []):
            led_to = before.get("led_to")
            if led_to and led_to.split("#")[0] != (before["url"] or "").split("#")[0] and not self._repeats_ok(now):
                if before["shape"] == now["shape"] and now["url"] != before["url"]:
                    state = self.state
                    state["status"] = "done"
                    state["stop_reason"] = "already_followed"
                    write_event(
                        {
                            "event": "done",
                            "goal": state.get("goal"),
                            "reason": "already_followed",
                            "label": now["label"],
                            "why": REASON_WHY["already_followed"],
                        }
                    )
                    return True
            if before["url"] != now["url"]:
                continue
            same = (before["node"] is not None and before["node"] == now["node"]) or (
                abs(before["x"] - now["x"]) < 8 and abs(before["y"] - now["y"]) < 8
            )
            if not same:
                continue
            if before["shape"] == now["shape"] and before["flags"] == now["flags"]:
                continue
            state = self.state
            state["status"] = "blocked"
            state["stop_reason"] = "toggle_undo"
            write_event(
                {
                    "event": "blocked",
                    "goal": state.get("goal"),
                    "reason": "toggle_undo",
                    "label": now["label"],
                    "why": f"{REASON_WHY['toggle_undo']} Was {before['label']!r}, now {now['label']!r}.",
                }
            )
            return True
        return False

    def _note_unexecuted(self, exc: BaseException | None = None) -> bool:
        """Count decisions that raised before anything was performed. Two on the same target stops the run."""
        decision = (self.state.get("decisions") or [None])[-1] or {}
        operation = decision.get("operation")
        if operation not in {"TYPE_TEXT", "CLICK", "SELECT", "PRESS_ENTER"}:
            return False
        key = (operation, decision.get("target") or decision.get("choice"))
        if key == getattr(self, "_unexecuted_key", None):
            self._unexecuted_count = getattr(self, "_unexecuted_count", 0) + 1
        else:
            self._unexecuted_key = key
            self._unexecuted_count = 1
        if self._unexecuted_count < 2:
            return False
        message = str(exc or "")
        if "covered" in message:
            reason = "covered_target"
        elif operation == "CLICK":
            reason = "click_not_sent"
        elif "Field changed" in message:
            reason = "field_changed"
        else:
            reason = "stale_page"
        self.state["decision"] = None
        self.state["status"] = "blocked"
        self.state["stop_reason"] = reason
        write_event(
            {
                "event": "blocked",
                "goal": self.state.get("goal"),
                "reason": reason,
                "why": REASON_WHY[reason],
                "kind": operation,
            }
        )
        return True

    LOW_CONFIDENCE_STRIKES = 2

    def _stop_low_confidence(self):
        """Stop on the second consecutive degenerate tick. The decision is discarded, never executed.

        A degenerate operation spread is the model reporting that it sees no reason to prefer
        anything: the top operation is low and the gap to the runner-up is narrow, so whichever
        one it returns is close to a coin flip. One such tick is ordinary uncertainty and the
        next observation may settle it, so a single one never stops. Two in a row means the run
        is choosing what to click and type on the user's own profile by chance, and the cheapest
        honest end is to stop before the second one lands.

        The streak counts consecutive ticks, not decisions in total: a tick with a real preference
        resets it. It is read here, ahead of every other rejection path, so nothing below can
        execute a decision on its way out and no path can settle the run on a coin flip instead.
        """
        if not degenerate(self.state.get("decision")):
            self._degenerate_streak = 0
            return None
        self._degenerate_streak = getattr(self, "_degenerate_streak", 0) + 1
        if self._degenerate_streak < self.LOW_CONFIDENCE_STRIKES:
            return None
        state = self.state
        state["decision"] = None
        state["status"] = "blocked"
        state["stop_reason"] = "low_confidence"
        write_event(
            {
                "event": "blocked",
                "goal": state.get("goal"),
                "reason": "low_confidence",
                "why": REASON_WHY["low_confidence"],
            }
        )
        return self.snapshot()

    def _reject_weak_done(self):
        """Do not finish on a weak DONE, on a zero-action DONE, or on a label-only page."""
        state = self.state
        decision = state.get("decision")
        page = state.get("page") or {}
        if not decision or decision.get("choice") != "DONE":
            return None
        # `history` is the run's performed-action log: `_remember_click` appends
        # only when an action actually went out, so its length is the count of
        # actions executed, not the count of decisions made.
        performed = len(state.get("history") or [])
        # A run whose clock is already spent cannot click or type anything else, so
        # refusing its zero-action DONE can only convert a finish into a failure.
        # Exempt that case (DONE_MIN still applies); the certainty requirement is
        # aimed at runs that still had options and spent none of them.
        effective = None if (performed == 0 and self._time_budget_spent()) else performed
        if done_acceptable(decision, page, executed_actions=effective):
            return None
        # The navigation bypass: a DONE below `DONE_MIN` may still end the run when
        # the run got itself to the right page. `moved_on` alone does not say "right".
        # M7 DONE'd at 0.56 on the event page with one click and no scroll, and a bare
        # path difference accepted it; `moved_on` is a comparison of two strings, not a
        # claim about the goal, and the goal named an order book the run never scrolled to.
        # So the bypass now asks `end_state_reached` -- the same goal-progress bar
        # `_blocked_rescue` already applies to a BLOCKED -- which requires the
        # destination to carry the goal's own words and the run to have performed a
        # non-scroll step for each step the goal names. Its `moved_on` and shell clauses
        # are the ones this condition used to spell out by hand.
        if _top_operation(decision) == "DONE" and end_state_reached(
            page,
            goal=state.get("goal"),
            history=state.get("history"),
            moved_on=self._moved_on(page),
        ):
            return None
        state["decision"] = None
        text = page.get("text") or ""
        probability = done_probability(decision)
        write_event(
            {
                "event": "reject_done",
                "goal": state.get("goal"),
                "why": f"DONE p={probability:.2f} was not accepted",
                "url": page.get("url"),
            }
        )
        if page_is_shell(text):
            browser = state.get("browser")
            for _ in range(6):
                if browser is None:
                    break
                browser.sleep(getattr(browser, "HYDRATE_SLEEP_S", 0) or 0)
                state["page"] = browser._observe_once(screenshot=False)
                if not page_is_shell((state["page"] or {}).get("text") or ""):
                    state["status"] = "ready"
                    return self.snapshot()
            state["status"] = "blocked"
            state["stop_reason"] = "shell"
            return self.snapshot()
        self._weak_done = getattr(self, "_weak_done", 0) + 1
        if self._weak_done >= 2:
            state["status"] = "blocked"
            state["stop_reason"] = "weak_done"
            return self.snapshot()
        scroll = next((item for item in (page.get("actions") or []) if item.get("id") == "scroll_down"), None)
        browser = state.get("browser")
        if scroll is not None and browser is not None:
            try:
                browser.act(scroll, page)
            except StalePage:
                self.metrics.record_stale()
            state["page"] = browser.observe(screenshot=self.screenshots)
            history = state.setdefault("history", [])
            history.append(
                {
                    "step": len(history) + 1,
                    "action": "Scroll down",
                    "kind": "scroll",
                    "operation": "SCROLL_DOWN",
                    "page_changed": True,
                }
            )
        elif browser is not None:
            browser.sleep(getattr(browser, "HYDRATE_SLEEP_S", 0) or 0)
            state["page"] = browser._observe_once(screenshot=False)
        state["status"] = "ready"
        return self.snapshot()

    LOOK_SCROLLS = 3

    def _moved_on(self, page: dict) -> bool:
        """This run already took the user to another page, so the goal's action has happened."""
        start = getattr(self, "_start_url", None)
        url = (page or {}).get("url")
        return bool(start and url) and start.split("#")[0] != url.split("#")[0]

    def _blocked_rescue(self, page: dict):
        """Last chance for a BLOCKED that this run had already earned its way out of.

        Called only where `_look_further` has stopped helping — the scroll budget
        is spent, or the page offers nothing to scroll. That ordering is the
        conservatism: scrolling is tried first, on every BLOCKED, so this cannot
        pre-empt a rescue that a scroll would have found anyway.

        A BLOCKED the model is right about (the target is still below the fold,
        the page is a shell) fails `end_state_reached` and the run stops blocked
        exactly as before. Only a BLOCKED on a page this run navigated to, that
        visibly carries the goal's own words, and that this run acted to reach,
        is converted — and it converts to `done` with its own stop_reason, never
        to a silent success.
        """
        state = self.state
        decision = state.get("decision") or {}
        if decision.get("choice") != "BLOCKED":
            return None
        if not end_state_reached(
            page,
            goal=state.get("goal"),
            history=state.get("history"),
            moved_on=self._moved_on(page),
        ):
            return None
        state["decision"] = None
        state["status"] = "done"
        state["stop_reason"] = "end_state_reached"
        if state.get("elapsed_ms") is None:
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        write_event(
            {
                "event": "done",
                "goal": state.get("goal"),
                "status": "done",
                "reason": "end_state_reached",
                "kind": "BLOCKED",
                "url": (page or {}).get("url"),
                "why": REASON_WHY["end_state_reached"],
            }
        )
        return self.snapshot()

    def _look_further(self):
        """BLOCKED usually means the target is below the viewport. Scroll and ask again, a few times."""
        state = self.state
        decision = state.get("decision") or {}
        if decision.get("choice") != "BLOCKED":
            return None
        looked = getattr(self, "_looked", 0)
        page = state.get("page") or {}
        browser = state.get("browser")
        if looked >= self.LOOK_SCROLLS or browser is None:
            return self._blocked_rescue(page)
        text = page.get("text") or ""
        if page_is_shell(text) or len(text.strip()) < 160:
            self._looked = looked + 1
            state["decision"] = None
            for _ in range(8):
                browser.sleep(getattr(browser, "HYDRATE_SLEEP_S", 0) or 0)
                state["page"] = browser._observe_once(screenshot=False)
                now = (state["page"] or {}).get("text") or ""
                if not page_is_shell(now) and len(now.strip()) >= 160:
                    break
            state["status"] = "ready"
            write_event(
                {
                    "event": "look_further",
                    "goal": state.get("goal"),
                    "why": "Model chose BLOCKED on a page still loading; waited.",
                }
            )
            return self.snapshot()
        scroll = next((item for item in page.get("actions") or [] if item.get("id") == "scroll_down"), None)
        if scroll is None:
            return self._blocked_rescue(page)
        self._looked = looked + 1
        state["decision"] = None
        try:
            browser.act(scroll, page)
        except StalePage:
            self.metrics.record_stale()
        state["page"] = browser.observe(screenshot=getattr(self, "screenshots", False))
        history = state.setdefault("history", [])
        history.append(
            {
                "step": len(history) + 1,
                "action": "Scroll down",
                "kind": "scroll",
                "operation": "SCROLL_DOWN",
                "page_changed": state["page"].get("fingerprint") != page.get("fingerprint"),
                "usage": decision.get("usage") or {},
            }
        )
        state["status"] = "ready"
        write_event(
            {
                "event": "look_further",
                "goal": state.get("goal"),
                "why": f"Model chose BLOCKED; scrolled to look for the target ({self._looked}/{self.LOOK_SCROLLS}).",
            }
        )
        return self.snapshot()

    def _maybe_unblock_scroll(self, snap, before_y):
        state = self.state
        if state.get("status") != "blocked":
            return snap
        history = state.get("history") or []
        last_three = history[-3:]
        if len(last_three) < 3 or any(h.get("kind") != "scroll" for h in last_three):
            return snap
        new_y = ((state.get("page") or {}).get("scroll") or {}).get("y")
        if before_y is None or new_y is None or new_y == before_y:
            return snap
        state["status"] = "ready"
        return self.snapshot()

    def _hud_payload(self):
        state = self.state or {}
        decision = state.get("decision") or ((state.get("decisions") or [None])[-1]) or {}
        page = state.get("page") or {}
        _elements, targets, _controls = action_space(page.get("actions") or [])
        label_by_index = {}
        node_by_index = {}
        for group in targets.values():
            for index, action in group.items():
                label_by_index.setdefault(index, (action.get("label") or "").split(" → ")[0][:60])
                if action.get("node") is not None:
                    node_by_index.setdefault(index, action["node"])
        questions = ((decision.get("request") or {}).get("questions") or {})
        op_key = (decision.get("operation") or "").lower() + "_target"
        criteria = (questions.get(op_key) or {}).get("criteria") or {}

        def index_label(index):
            return label_of(criteria.get(index), label_by_index.get(index) or index, 60)

        target_key = decision.get("target")
        target_probs = decision.get("target_probabilities") or {}
        same_page = not decision.get("fingerprint") or decision.get("fingerprint") == page.get("fingerprint")
        marks = []
        if same_page:
            for index, prob in sorted(target_probs.items(), key=lambda kv: -float(kv[1] or 0))[:8]:
                node = node_by_index.get(index)
                if node is None:
                    continue
                marks.append(
                    {
                        "node": node,
                        "label": index_label(index),
                        "p": round(float(prob), 3),
                        "chosen": index == target_key,
                    }
                )
        steps = []
        for item in (state.get("history") or [])[-12:]:
            text = item.get("text") or ""
            steps.append(
                {
                    "n": item.get("step"),
                    "op": item.get("operation") or item.get("kind"),
                    "label": (item.get("action") or "").split(" → ")[0][:60],
                    "p": round(float(item.get("probability") or 0), 3),
                    "changed": item.get("page_changed"),
                    "text": text[:40] or None,
                }
            )
        op_probs = decision.get("operation_probabilities") or {}
        operation = decision.get("operation") or decision.get("choice") or ""
        target_label = index_label(target_key) if target_key else ""
        if operation in {"DONE", "BLOCKED", "WAIT", "SCROLL_UP", "SCROLL_DOWN"}:
            target_label = ""
        last = ((state.get("history") or [None])[-1]) or {}
        status = state.get("status") or ""
        why = _why(status, decision, state.get("history") or [], state.get("stop_reason"))
        history = state.get("history") or []
        usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        for item in history:
            row = item.get("usage") or {}
            usage["input_tokens"] += int(row.get("input_tokens") or 0)
            usage["output_tokens"] += int(row.get("output_tokens") or 0)
            usage["cost"] += float(row.get("cost") or 0)
        if status == "predicted":
            row = decision.get("usage") or {}
            usage["input_tokens"] += int(row.get("input_tokens") or 0)
            usage["output_tokens"] += int(row.get("output_tokens") or 0)
            usage["cost"] += float(row.get("cost") or 0)
        return {
            "goal": state.get("goal") or "",
            "status": status,
            "reason": state.get("stop_reason") or "",
            "operation": operation,
            "confidence": decision.get("confidence"),
            "target_label": target_label,
            "last_action": last.get("action") or "",
            "why": why,
            "degenerate": degenerate(decision),
            "ops": _ranked(op_probs),
            "targets": [
                [index_label(key), round(float(val), 3)]
                for key, val in sorted(target_probs.items(), key=lambda kv: -float(kv[1] or 0))[:6]
            ],
            "steps": steps,
            "marks": marks,
            "url": (page.get("url") or "")[:180],
            "step": len(history),
            "usage": usage,
        }

    def _paint_hud(self):
        browser = (self.state or {}).get("browser")
        if browser is None or not getattr(self, "debug", False):
            return
        browser.paint_hud(self._hud_payload())
