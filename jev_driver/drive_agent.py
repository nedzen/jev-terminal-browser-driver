"""Driver-side Agent subclass. agent.py stays verbatim."""

from .agent import Agent


class DriveAgent(Agent):
    """Exempt advancing scrolls from the 3-repeat guard; optional debug HUD."""

    def __init__(self, url, goals, *, debug=False, **kwargs):
        super().__init__(url, goals, **kwargs)
        self.debug = debug
        browser = self.state.get("browser")
        if browser is not None:
            browser.debug = debug
            if debug:
                browser.paint_hud(self._hud_payload())

    def command(self, name, body=None):
        if name == "act":
            before_y = ((self.state.get("page") or {}).get("scroll") or {}).get("y")
            snap = super().command("act", body)
            snap = self._maybe_unblock_scroll(snap, before_y)
            self._paint_hud()
            return snap
        snap = super().command(name, body)
        if name in {"predict", "tick"}:
            self._paint_hud()
        return snap

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
        decision = state.get("decision") or (state.get("decisions") or [None])[-1] or {}
        ops = sorted((decision.get("operation_probabilities") or {}).items(), key=lambda kv: -kv[1])
        targets = sorted((decision.get("target_probabilities") or {}).items(), key=lambda kv: -kv[1])[:8]
        return {
            "goal": state.get("goal") or "",
            "operation": decision.get("operation") or decision.get("choice"),
            "confidence": decision.get("confidence"),
            "target": decision.get("target") or decision.get("choice"),
            "ops": ops,
            "targets": targets,
        }

    def _paint_hud(self):
        browser = (self.state or {}).get("browser")
        if browser is None or not getattr(self, "debug", False):
            return
        browser.paint_hud(self._hud_payload())
