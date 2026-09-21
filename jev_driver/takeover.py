"""Watch-mode lease: yield when the user navigates the visible pane. agent.py stays verbatim."""

import time

from .agent import Agent
from .browser import StalePage

TAKEOVER_REASON = "user took over the browser"


class WatchAgent(Agent):
    """Same loop as Agent, but a stale page means the user took over — do not re-observe and continue."""

    def command(self, name, body=None):
        if name != "tick":
            return super().command(name, body)
        state = self.state
        if not state["browser"].fresh(state["page"]):
            return self._yield_takeover()
        try:
            self.command("predict", {})
            if not state["browser"].fresh(state["page"]):
                return self._yield_takeover()
            return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
        except StalePage:
            return self._yield_takeover()

    def _yield_takeover(self):
        self.state["decision"] = None
        self.state["status"] = "blocked"
        self.state["takeover"] = True
        if self.state["started_at"] is not None:
            self.state["elapsed_ms"] = round((time.perf_counter() - self.state["started_at"]) * 1000)
        return self.snapshot()
