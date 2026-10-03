import importlib.util
from pathlib import Path

import pytest

from jev_driver import lease, runlog
from jev_driver.browser import StalePage, fingerprint
from plugin.core import env as core_env

ROOT = Path(__file__).resolve().parents[1]

# Default control the agent-loop doubles expose unless a test overrides `actions`.
DEFAULT_FAKE_ACTION = {
    "id": "e1",
    "kind": "click",
    "label": "Open Widget",
    "role": "button",
    "value": "",
    "node": 7,
}


@pytest.fixture()
def load_mcp():
    """Execute scripts/mcp.py under a module name the caller picks.

    The name is the caller's because `spec.loader.exec_module` is what binds
    the module, and a shared name would hand the next test the one that ran
    first instead of a fresh server.
    """

    def _load(name):
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "mcp.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    return _load


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Tests must never touch the user's real run log or remembered tab.

    Patches both runlog paths and plugin handler LOG_DIR: log_handler_event
    writes through core_env.LOG_DIR, and patching only the handler re-export
    left the real ~/.cache/wwwdrive open.
    """
    log_dir = tmp_path / "run-log"
    monkeypatch.setattr(runlog, "LOG_DIR", log_dir)
    monkeypatch.setattr(runlog, "JSONL_PATH", log_dir / "drive.jsonl")
    monkeypatch.setattr(runlog, "TEXT_PATH", log_dir / "drive.log")
    monkeypatch.setattr(lease, "LAST_PAGE_PATH", log_dir / "last-page.json")
    monkeypatch.setattr(lease, "HUD_STATE_PATH", log_dir / "hud.json")
    monkeypatch.setattr(core_env, "LOG_DIR", log_dir)


class Clock:
    """Monotonic clock the test drives by hand, so no wall time is spent."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def perf_counter(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class _Time:
    """Stands in for the time module production code reads its clock from."""

    def __init__(self, clock):
        self.perf_counter = clock.perf_counter


class FakeBrowser:
    """Agent-loop browser double: page snapshots and recorded mutations.

    Shared by time-budget, metrics, and low-confidence tests. Specialised
    doubles subclass this and override ``_page`` / ``act`` as needed.

    Not used by freshness (extends real Browser + evaluate queue) or final_view
    (observe-only page queue) — those APIs are too different to unify safely.
    """

    HYDRATE_SLEEP_S = 0

    def __init__(
        self,
        url,
        clock=None,
        *,
        actions=None,
        stable=False,
        stale_acts=0,
        keep_after_reads=None,
        costs=None,
        act_keys=("id", "label"),
        count_reads_in_page=False,
        fixed_url=False,
        growing_text=False,
    ):
        self.url = url
        self.clock = clock
        self.debug = False
        self.actions = [dict(item) for item in (actions if actions is not None else (DEFAULT_FAKE_ACTION,))]
        self.stable = stable
        self.stale_acts = stale_acts
        self.keep_after_reads = keep_after_reads
        self.costs = dict(costs or {})
        self.act_keys = tuple(act_keys)
        self.count_reads_in_page = count_reads_in_page
        self.fixed_url = fixed_url
        self.growing_text = growing_text
        self.acts = []
        self.reads = 0
        self.closed = False
        self.huds = []

    def _charge(self, phase):
        amount = self.costs.get(phase)
        if self.clock is not None and amount is not None:
            self.clock.advance(amount)

    def _page_url(self):
        if self.fixed_url or self.stable:
            return self.url
        # metrics increments reads inside _page; time_budget increments after.
        if self.count_reads_in_page:
            return f"{self.url}#read{self.reads}"
        return self.url if self.reads == 0 else f"{self.url}#read{self.reads}"

    def _page_text(self):
        base = "The widget list is here with the panel control at the top of the page."
        if self.growing_text:
            return base + " " + "." * self.reads
        return base

    def _page_actions(self):
        if self.keep_after_reads is not None and self.reads > self.keep_after_reads:
            return []
        return [dict(item) for item in self.actions]

    def _page(self):
        if self.count_reads_in_page:
            self.reads += 1
        page = {
            "url": self._page_url(),
            "title": "Widgets",
            "text": self._page_text(),
            "scroll": {"x": 0, "y": 0, "height": 900},
            "actions": self._page_actions(),
        }
        page["fingerprint"] = fingerprint(page)
        return page

    def observe(self, screenshot=True):
        self._charge("observe")
        page = self._page()
        if not self.count_reads_in_page:
            self.reads += 1
        return page

    def _observe_once(self, screenshot=False):
        if self.count_reads_in_page:
            # metrics: retry path pays the observe cost again via observe().
            return self.observe(screenshot=screenshot)
        page = self._page()
        self.reads += 1
        return page

    def fresh(self, page, action=None, kind=None):
        self._charge("fresh")
        return True

    def act(self, action, page, text=None, **kw):
        self.acts.append({key: action.get(key) for key in self.act_keys})
        self._charge("act")
        if self.stale_acts > 0:
            self.stale_acts -= 1
            raise StalePage("Page changed since this decision. Observe again.")

    def sleep(self, seconds):
        self._charge("sleep")
        return None

    def paint_hud(self, payload):
        self.huds.append(payload)

    def close(self):
        self.closed = True
