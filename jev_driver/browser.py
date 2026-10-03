"""Observed actions through terminal-browser CDP; one session, no per-step subprocess.

Split across four modules: `lease.py` owns the tab lease and last-page memory,
`probe.py` owns fingerprinting and the read-only freshness probe, `ops.py` owns
the CDP-level read/act executor, and this module keeps the `Browser` class plus
everything a `Browser` method calls as a module-level name — imported here so a
test that monkeypatches `jev_driver.browser.X` still reaches the call site inside
a `Browser` method. Pure helpers and constants are re-exported for import
convenience; `jev_driver.lease` is the source of truth for the mutable lease
state (`LAST_CONTINUITY`, `LAST_PAGE_PATH`, `HUD_STATE_PATH`).
"""

import json
import time
from pathlib import Path

from . import lease
from .cdp import cdp, connect, list_browsers
from .lease import (
    LAST_PAGE_KEYS,
    LEASE,
    PROVENANCE_KEY,
    _attach,
    _get_targets,
    _is_ephemeral_url,
    _json_pages,
    _load_last_page,
    _log_continuity,
    _netloc_id,
    _open_owned_tab,
    _target_ok,
    _unique_url,
    find_continuable_page,
    hud_open,
    remember_page,
    save_hud_open,
    set_lease,
)
from .ops import _offer_enter, browser_operation
from .probe import (
    PROBE_REASONS,
    PROBE_RETRIES,
    _clickable,
    _probe_expression,
    _probe_flags,
    _probe_reason,
    _probe_telemetry,
    _same_document,
    _same_href,
    _same_marker,
    _same_target,
    _strip_jev_marker,
    fingerprint,
    without_counts,
)
from .readiness import page_is_shell
from .runlog import write_event

__all__ = [
    "Browser",
    "StalePage",
    "READ_STATE",
    "HUD_JS",
    "MARKER",
    "LEASE",
    "LAST_PAGE_KEYS",
    "PROVENANCE_KEY",
    "PROBE_REASONS",
    "PROBE_RETRIES",
    "cdp",
    "connect",
    "list_browsers",
    "write_event",
    "page_is_shell",
    "set_lease",
    "find_continuable_page",
    "remember_page",
    "hud_open",
    "save_hud_open",
    "browser_operation",
    "fingerprint",
    "without_counts",
    "_attach",
    "_get_targets",
    "_is_ephemeral_url",
    "_json_pages",
    "_load_last_page",
    "_log_continuity",
    "_netloc_id",
    "_open_owned_tab",
    "_target_ok",
    "_unique_url",
    "_offer_enter",
    "_clickable",
    "_probe_expression",
    "_probe_flags",
    "_probe_reason",
    "_probe_telemetry",
    "_same_document",
    "_same_href",
    "_same_marker",
    "_same_target",
    "_strip_jev_marker",
]

READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
HUD_JS = Path(__file__).with_name("hud.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""

def __getattr__(name):
    """Forward reads of lease's rebound globals, so `browser.X` always sees the live value.

    `LEASE` is a dict mutated in place, so the import above already shares one
    object with `lease.LEASE`. These four are reassigned with `=` instead (plain
    module globals, not containers), so a `from .lease import X` above would copy
    the value at import time and go stale the moment `lease.X` is reassigned —
    by production code (`LAST_CONTINUITY`) or by a test's `monkeypatch.setattr`.
    """
    if name in {"LAST_CONTINUITY", "LAST_PAGE_PATH", "HUD_STATE_PATH", "LAST_PAGE_TTL_S"}:
        return getattr(lease, name)
    raise AttributeError(name)

class Browser:
    HYDRATE_MIN_ACTIONS = 8
    HYDRATE_MAX_ROUNDS = 3
    HYDRATE_SHELL_ROUNDS = 8
    HYDRATE_SLEEP_S = 0.4
    sleep = staticmethod(time.sleep)
    # Why the last fresh() call said no. Written by fresh(), read by act() for telemetry.
    _fresh_reason = None
    # The same call's diagnostic half. Last attempt wins: a probe that ends on
    # "not_actionable" is the one whose numbers explain it.
    _probe_detail: dict = {}

    def __init__(self, url):
        connect()
        self.owned = False
        self.target = None
        self.session = None
        self.debug = False
        self._needs_hydrate = True
        if LEASE["tab"] == "target":
            target_id = LEASE["target_id"]
            if not target_id:
                raise ValueError("Lease tab=target requires target_id")
            info = next((t for t in _get_targets() if t["targetId"] == target_id), None)
            if info is None:
                page = next((p for p in _json_pages() if p.get("id") == target_id), None)
                if page:
                    info = {
                        "targetId": target_id,
                        "type": page.get("type") or "page",
                        "url": page.get("url") or "",
                    }
            if info is None:
                raise RuntimeError(f"CDP target {target_id} not found")
            if not _target_ok(info, allow_denylist=True):
                raise RuntimeError(f"Refusing to attach to target type/url {info.get('type')} {info.get('url')}")
            self.target = target_id
            self.owned = False
            self.session = _attach(self.target)
            if LEASE["navigate"]:
                self._keep_hud_toggle()
                self._navigate(url)
        else:
            self.target, self.owned = _open_owned_tab(url)
            self.session = _attach(self.target)
            if LEASE["create_fallback"] and LEASE["navigate"]:
                self._navigate(url)
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)
        try:
            remember_page(self.target, self.evaluate("location.href") or url)
        except StalePage:
            remember_page(self.target, url)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def _install_hud(self):
        """Repaint the saved panel on every new document in this tab while attached."""
        if getattr(self, "_hud_installed", False) or not self.session:
            return
        source = (
            HUD_JS
            + "\nif (document.readyState === 'loading') {"
            " document.addEventListener('DOMContentLoaded', () => window.__jevHudRestore()); }"
            " else { window.__jevHudRestore(); }"
        )
        try:
            self.call("Page.addScriptToEvaluateOnNewDocument", source=source)
            self._hud_installed = True
        except RuntimeError:
            return

    def _navigate(self, url, timeout=15):
        """Page.navigate returns before the old document is gone. Wait for the new one."""
        try:
            before, href = self.evaluate("[performance.timeOrigin, location.href]")
        except (RuntimeError, StalePage, TypeError, ValueError):
            before, href = None, ""
        if (href or "").split("#")[0] == url.split("#")[0] and "#" in url:
            before = None
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + timeout
        while before is not None and time.monotonic() < deadline:
            try:
                if self.evaluate("performance.timeOrigin") != before:
                    return
            except (RuntimeError, StalePage):
                pass
            time.sleep(0.05)

    def _keep_hud_toggle(self):
        """The user may have toggled the panel since the last paint. Save it before the document goes away."""
        try:
            opened = self.evaluate("window.__jevHudOpen")
        except (RuntimeError, StalePage):
            return
        if isinstance(opened, bool) and opened != hud_open():
            save_hud_open(opened)

    def _hud_eval(self, expression):
        try:
            response = self.call("Runtime.evaluate", expression=HUD_JS + "\n" + expression, returnByValue=True)
        except (RuntimeError, StalePage):
            return
        opened = (response.get("result") or {}).get("value")
        if isinstance(opened, bool) and opened != hud_open():
            save_hud_open(opened)

    def paint_hud(self, payload):
        if not getattr(self, "debug", False) or not self.session:
            return
        self._install_hud()
        self._hud_eval("window.__jevHudPaint(" + json.dumps({**payload, "open": hud_open()}) + ");")

    def restore_hud(self):
        """Show the last saved panel without a new decision."""
        if not getattr(self, "debug", False) or not self.session:
            return
        self._install_hud()
        self._hud_eval("window.__jevHudRestore();")

    def _read_page(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      const overlay=action.kind==='click' && !!(
                        field?.getAttribute('role')==='combobox' ||
                        field?.getAttribute('aria-haspopup') ||
                        /date|depart|return|calendar|picker|check-in|check-out/i.test(String(action.label||''))
                      );
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish, overlay ? 450 : autocomplete ? 200 : 50);
                      const visible=e=>{
                        const r=e.getBoundingClientRect();
                        return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                          e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                      };
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        const popups=[...document.querySelectorAll('[role="dialog"],[role="listbox"],[role="grid"]')];
                        if (++frames>=2 && (
                          (!autocomplete && !overlay) ||
                          (autocomplete && options.some(visible)) ||
                          (overlay && (popups.some(visible) || options.some(visible) || frames>=12))
                        )) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        # A click can start a full page load. Give the next document time to exist.
        deadline = time.monotonic() + 10
        attempt = 0
        while True:
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                attempt += 1
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02 if attempt < 10 else 0.15)

    def _observe_once(self, screenshot=True):
        page = self._read_page(screenshot)
        _offer_enter(page)
        return page

    def _settle_observe(self, screenshot=True):
        prev_len = -1
        page = None
        limit = max(1, int(self.HYDRATE_MAX_ROUNDS))
        rounds = 0
        while rounds < limit:
            rounds += 1
            page = self._observe_once(screenshot)
            text = page.get("text") or ""
            n_actions = len(page.get("actions") or [])
            if page_is_shell(text):
                limit = max(limit, int(self.HYDRATE_SHELL_ROUNDS))
                if rounds >= limit:
                    return page
                prev_len = len(text)
                if self.HYDRATE_SLEEP_S:
                    self.sleep(self.HYDRATE_SLEEP_S)
                continue
            if n_actions >= self.HYDRATE_MIN_ACTIONS and (prev_len < 0 or len(text) <= prev_len):
                return page
            prev_len = len(text)
            if self.HYDRATE_SLEEP_S:
                self.sleep(self.HYDRATE_SLEEP_S)
        return page

    def observe(self, screenshot=True):
        if getattr(self, "_needs_hydrate", False) and self.HYDRATE_MAX_ROUNDS > 1:
            page = self._settle_observe(screenshot)
            self._needs_hydrate = False
            return page
        return self._observe_once(screenshot)

    def fresh(self, page, action=None, *, reprobe=True):
        kind = (action or {}).get("kind")
        if kind in {"click", "select", "fill"}:
            node = action["node"]
            if type(node) is not int:
                self._fresh_reason = "target_detached"
                return False
            self._fresh_reason = self._probe_target(kind, page, node, reprobe=reprobe)
            return self._fresh_reason == "ok"
        if kind in {"scroll", "wait", "done"}:
            current = self.evaluate("(() => [performance.timeOrigin, location.href])()")
            self._fresh_reason = "ok" if _same_document(page, current) else "target_changed"
            return self._fresh_reason == "ok"
        self._fresh_reason = "ok" if _same_marker(page["marker"], self.evaluate(MARKER)) else "target_changed"
        return self._fresh_reason == "ok"

    def _probe_target(self, kind, page, node, *, reprobe=True) -> str:
        """Probe until actionable (or detached). ``reprobe=False`` skips the settle window."""
        expression = _probe_expression(node)
        reason = "target_changed"
        attempts = len(PROBE_RETRIES) if reprobe else 0
        for attempt in range(attempts + 1):
            if attempt:
                self.sleep(PROBE_RETRIES[attempt - 1])
            current = self.evaluate(expression)
            self._probe_detail = _probe_telemetry(current)
            reason = _probe_reason(kind, page, node, current)
            if reason in {"ok", "target_detached"}:
                break
        return reason

    def act(self, action, page, text=None, *, reprobe=True):
        """Execute one action after the freshness probe. ``reprobe=False`` probes once."""
        if not self.fresh(page, action, reprobe=reprobe):
            kind = action.get("kind")
            reason = "field_changed" if kind in {"click", "select", "fill"} else "page_changed"
            write_event(
                {
                    "event": "stale",
                    "kind": kind,
                    "label": action.get("label"),
                    "reason": reason,
                    # field_changed/page_changed says which guard failed; this says why the
                    # target was not usable, which is the part worth fixing.
                    "probe_reason": self._fresh_reason,
                    # Which half of `live` went false, so a not_actionable is
                    # attributable (disabled vs. opacity-0 vs. unboxed) instead of
                    # being one opaque word. Absent when the probe carried none.
                    "probe": dict(self._probe_detail) or None,
                    "why": (
                        "The target changed before input."
                        if reason == "field_changed"
                        else "The page changed before input."
                    ),
                }
            )
            message = (
                "Field changed since this decision. Observe again."
                if reason == "field_changed"
                else "Page changed since this decision. Observe again."
            )
            raise StalePage(message)
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        write_event(
            {
                "event": "act",
                "kind": action.get("kind"),
                "label": action.get("label"),
                "via": (result or {}).get("via") if isinstance(result, dict) else action.get("kind"),
                "typed": (text or "")[:80] or None,
            }
        )
        self.after_input = action if action["kind"] != "wait" else None
        if action["kind"] in {"click", "select", "fill"}:
            self._needs_hydrate = True
        return result

    def close(self):
        # Detach only — closing a TUI tab destroys Electron webContents under PageHost.
        if self.session:
            try:
                cdp("Target.detachFromTarget", sessionId=self.session)
            except RuntimeError:
                pass
        self.target = None
        self.session = None
