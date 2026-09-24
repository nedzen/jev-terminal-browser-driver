"""Driver-side Agent subclass. agent.py stays verbatim."""

from .agent import Agent
from .browser import StalePage
from .model import action_space, field_context, field_text
from .readiness import REASON_WHY, done_acceptable, done_probability, page_is_shell
from .runlog import write_event


def _label_stem(label: str) -> str:
    """Drop a leading count so '12 comments' still matches '13 comments'."""
    parts = label.split(None, 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1].strip()
    return label.strip()


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


def _ranked(probs, limit=6):
    items = sorted((probs or {}).items(), key=lambda kv: -float(kv[1] or 0))
    return [[str(key), round(float(val), 3)] for key, val in items[:limit]]


class DriveAgent(Agent):
    """Exempt advancing scrolls from the 3-repeat guard; optional debug HUD."""

    def __init__(self, url, goals, *, debug=False, **kwargs):
        super().__init__(url, goals, **kwargs)
        self.debug = debug
        self._unexecuted_key = None
        self._unexecuted_count = 0
        self._weak_done = 0
        browser = self.state.get("browser")
        if browser is not None:
            browser.debug = debug
            if debug:
                browser.paint_hud(self._hud_payload())

    def command(self, name, body=None):
        if name == "act":
            rejected = self._reject_weak_done()
            if rejected is not None:
                self._paint_hud()
                return rejected
            before_y = ((self.state.get("page") or {}).get("scroll") or {}).get("y")
            try:
                snap = super().command("act", body)
            except StalePage as exc:
                decision = (self.state.get("decisions") or [None])[-1] or {}
                write_event(
                    {
                        "event": "stale",
                        "goal": self.state.get("goal"),
                        "kind": decision.get("operation"),
                        "label": self._decision_label(decision) or decision.get("target"),
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
                raise
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
        if state.get("status") != "blocked" or state.get("stop_reason"):
            return
        decision = (state.get("decisions") or [None])[-1] or {}
        if decision.get("choice") == "BLOCKED" or decision.get("operation") == "BLOCKED":
            state["stop_reason"] = "model_blocked"

    def _decision_label(self, decision: dict) -> str:
        operation = (decision.get("operation") or "").lower()
        questions = ((decision.get("request") or {}).get("questions") or {})
        criteria = (questions.get(operation + "_target") or {}).get("criteria") or {}
        raw = str(criteria.get(decision.get("target")) or "")
        if raw.startswith("[") and "]" in raw:
            raw = raw.split("]", 1)[1]
        return raw.split(";")[0].strip()

    def _retry_fill(self, decision: dict):
        """The feed changed during the decision. Type into the same label on a fresh read."""
        if (decision or {}).get("operation") != "TYPE_TEXT":
            return None
        label = self._decision_label(decision)
        if not label:
            return None
        state = self.state
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
            try:
                browser.act(action, page, text=text)
                break
            except StalePage:
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
        label = self._decision_label(decision)
        if not label:
            return None
        state = self.state
        browser = state.get("browser")
        if browser is None:
            return None
        action = None
        for _ in range(2):
            page = browser._observe_once(screenshot=False)
            action = _click_named(page.get("actions") or [], label)
            if action is None:
                return None
            try:
                browser.act(action, page)
                break
            except StalePage:
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

    def _reject_weak_done(self):
        """Do not finish on a low-confidence DONE or on a page that is still only labels."""
        state = self.state
        decision = state.get("decision")
        page = state.get("page") or {}
        if not decision or decision.get("choice") != "DONE":
            return None
        if done_acceptable(decision, page):
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
                pass
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

        def label_of(index):
            text = str(criteria.get(index) or label_by_index.get(index) or index)
            if text.startswith("[") and "]" in text:
                text = text.split("]", 1)[1]
            return text.split(";")[0].strip()[:60]

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
                        "label": label_of(index),
                        "p": round(float(prob), 3),
                        "chosen": index == target_key,
                    }
                )
        steps = []
        for item in (state.get("history") or [])[-6:]:
            text = item.get("text") or ""
            steps.append(
                {
                    "n": item.get("step"),
                    "op": item.get("operation") or item.get("kind"),
                    "label": (item.get("action") or "")[:60],
                    "p": round(float(item.get("probability") or 0), 3),
                    "changed": item.get("page_changed"),
                    "text": text[:40] or None,
                }
            )
        op_probs = decision.get("operation_probabilities") or {}
        ranked = sorted((float(v) for v in op_probs.values()), reverse=True)
        top = ranked[0] if ranked else 0.0
        gap = top - (ranked[1] if len(ranked) > 1 else 0.0)
        operation = decision.get("operation") or decision.get("choice") or ""
        target_label = label_of(target_key) if target_key else ""
        if operation in {"DONE", "BLOCKED", "WAIT", "SCROLL_UP", "SCROLL_DOWN"}:
            target_label = ""
        last = ((state.get("history") or [None])[-1]) or {}
        status = state.get("status") or ""
        why = _why(status, decision, state.get("history") or [])
        return {
            "goal": state.get("goal") or "",
            "status": status,
            "operation": operation,
            "confidence": decision.get("confidence"),
            "target_label": target_label,
            "last_action": last.get("action") or "",
            "why": why,
            "degenerate": bool(op_probs) and top < 0.6 and gap < 0.1,
            "ops": _ranked(op_probs),
            "targets": [
                [label_of(key), round(float(val), 3)]
                for key, val in sorted(target_probs.items(), key=lambda kv: -float(kv[1] or 0))[:6]
            ],
            "steps": steps,
            "marks": marks,
            "url": (page.get("url") or "")[:180],
        }

    def _paint_hud(self):
        browser = (self.state or {}).get("browser")
        if browser is None or not getattr(self, "debug", False):
            return
        browser.paint_hud(self._hud_payload())
