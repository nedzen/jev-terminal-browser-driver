"""Observed actions through terminal-browser CDP; one session, no per-step subprocess."""

import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import discover as _discover
from .cdp import TB, cdp, cdp_port, connect, list_browsers
from .readiness import page_is_shell
from .runlog import write_event

READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
HUD_JS = Path(__file__).with_name("hud.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

BLOCKED_SCHEMES = ("chrome:", "chrome-untrusted:", "devtools:", "chrome-extension:")
DENYLIST_HOSTS = ("hindsight.vectorize.io",)

# Settle windows before a target counts as stale. A framework that re-parents a row, or a
# menu that re-renders, hands back the same node a frame later; re-reading one target is
# cheap, re-snapshotting the page is not (it rebuilds the action list this decision came from).
PROBE_RETRIES = (0.1, 0.2, 0.3)
# Why a decision can no longer touch the node it names. Telemetry only: act() still reports
# field_changed/page_changed, and these name the cause.
PROBE_REASONS = ("target_detached", "target_changed", "not_actionable", "not_writable", "ok")

LEASE = {
    "tab": "new",
    "target_id": None,
    "browser_key": None,
    "navigate": True,
    "create_fallback": None,
}

LAST_PAGE_PATH = Path.home() / ".cache" / "wwwdrive" / "last-page.json"
HUD_STATE_PATH = LAST_PAGE_PATH.parent / "hud.json"
LAST_PAGE_TTL_S = 1800
LAST_PAGE_KEYS = ("targetId", "url", "source", "browser_id", "ts")
LAST_CONTINUITY = None


def set_lease(*, tab="new", target_id=None, browser_key=None, navigate=True):
    LEASE.update(tab=tab, target_id=target_id, browser_key=browser_key, navigate=navigate, create_fallback=None)


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


def _pick_browser_key(data=None):
    data = data or list_browsers()
    browsers = data.get("browsers") or []
    if LEASE["browser_key"]:
        if not any(b.get("key") == LEASE["browser_key"] for b in browsers):
            raise RuntimeError(f"Unknown terminal-browser key {LEASE['browser_key']}")
        return LEASE["browser_key"]
    current = [b for b in browsers if b.get("inCurrentTab")]
    chosen = (current or browsers)[0]
    return chosen["key"]


def _target_ok(info, *, allow_denylist):
    if (info.get("type") or "page") != "page":
        return False
    url = info.get("url") or ""
    parsed = urlparse(url)
    if parsed.scheme in {s.rstrip(":") for s in BLOCKED_SCHEMES} or url.startswith(BLOCKED_SCHEMES):
        return False
    if not allow_denylist and parsed.hostname in DENYLIST_HOSTS:
        return False
    return True


def _is_ephemeral_url(url):
    text = url or ""
    if "jev-terminal-browser-driver/fixtures/" in text:
        return True
    if text.startswith(("about:", "chrome:", "devtools:", "chrome-untrusted:", "chrome-extension:")):
        return True
    return False


def _netloc_id(url: str) -> str:
    """Host:port from a ws/http URL. Bracket IPv6 so [::1]:9222 round-trips."""
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    if host:
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{port}" if port is not None else host
    return (parsed.netloc or "").lower()


def browser_identity():
    last = _discover.LAST
    if last is None:
        return "", ""
    source = last.source or ""
    ident = _netloc_id(last.ws_url) or _netloc_id(last.http_origin)
    return source, ident


def _log_continuity(reason: str) -> None:
    print(f"wwwdrive: continuity {reason}", file=sys.stderr)
    if reason != "lookup":
        write_event({"event": "continuity", "why": reason})


def _load_last_page():
    if not LAST_PAGE_PATH.is_file():
        return None
    try:
        data = json.loads(LAST_PAGE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or any(key not in data for key in LAST_PAGE_KEYS):
        return None
    return data


def _write_last_page(record: dict) -> None:
    LAST_PAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_PAGE_PATH.write_text(json.dumps(record))


def hud_open() -> bool:
    """Whether the user left the debug panel expanded. Survives navigation and new runs."""
    try:
        return json.loads(HUD_STATE_PATH.read_text()).get("open") is True
    except (OSError, ValueError, AttributeError):
        return False


def save_hud_open(opened: bool) -> None:
    try:
        HUD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        HUD_STATE_PATH.write_text(json.dumps({"open": bool(opened)}))
    except OSError:
        return


def remember_page(target_id, url):
    """Remember the driver's tab, including fixture URLs, so the next run can reuse it."""
    if not target_id:
        return
    source, browser_id = browser_identity()
    existing = _load_last_page()
    if LEASE.get("tab") == "target":
        if not existing or existing.get("targetId") != target_id:
            return
        existing["url"] = url or existing.get("url") or ""
        existing["ts"] = time.time()
        _write_last_page(existing)
        return
    _write_last_page(
        {
            "targetId": target_id,
            "url": url or "",
            "source": source,
            "browser_id": browser_id,
            "ts": time.time(),
        }
    )


def find_continuable_page():
    """Re-attach to a previously driven page when the caller omitted --url."""
    global LAST_CONTINUITY
    LAST_CONTINUITY = None
    existed = LAST_PAGE_PATH.is_file()
    remembered = _load_last_page()
    if remembered is None:
        if existed:
            LAST_CONTINUITY = "dropped:legacy-schema"
            _log_continuity("legacy-schema")
        return None, None
    try:
        age = time.time() - float(remembered["ts"])
    except (TypeError, ValueError):
        LAST_CONTINUITY = "dropped:legacy-schema"
        _log_continuity("legacy-schema")
        return None, None
    if age > LAST_PAGE_TTL_S:
        LAST_CONTINUITY = "dropped:ttl-expired"
        _log_continuity("ttl-expired")
        return None, None
    source, browser_id = browser_identity()
    try:
        pages = _json_pages()
    except (OSError, json.JSONDecodeError, TimeoutError):
        pages = []
    by_id = {p.get("id"): p for p in pages if p.get("type") == "page" and p.get("id")}
    remembered_id = remembered.get("targetId")
    same_label = (remembered.get("source"), remembered.get("browser_id")) == (source, browser_id)
    if not same_label:
        # The connected browser still has this tab. A host-string mismatch must not open another one.
        info = by_id.get(remembered_id) if remembered_id else None
        url = (info or {}).get("url") or ""
        if info and _target_ok({"type": "page", "url": url}, allow_denylist=False):
            _log_continuity(
                "re-attach despite label mismatch "
                f"remembered={remembered.get('source')}:{remembered.get('browser_id')} "
                f"connected={source}:{browser_id}"
            )
            LAST_CONTINUITY = "re-attach"
            return remembered_id, url
        LAST_CONTINUITY = "dropped:browser-mismatch"
        _log_continuity(
            "browser-mismatch "
            f"remembered={remembered.get('source')}:{remembered.get('browser_id')} "
            f"connected={source}:{browser_id}"
        )
        return None, None
    if remembered_id and remembered_id in by_id:
        info = by_id[remembered_id]
        url = info.get("url") or ""
        # The id is the driver's own tab, even when it is still on a fixture or an error page.
        if _target_ok({"type": "page", "url": url}, allow_denylist=False):
            LAST_CONTINUITY = "re-attach"
            return remembered_id, url
    live = []
    for page in pages:
        if page.get("type") != "page":
            continue
        url = page.get("url") or ""
        if _is_ephemeral_url(url):
            continue
        if not _target_ok({"type": "page", "url": url}, allow_denylist=False):
            continue
        live.append(page)
    if remembered.get("url"):
        stem = remembered["url"].split("?")[0].split("#")[0]
        for page in live:
            if (page.get("url") or "").split("?")[0].split("#")[0] == stem:
                LAST_CONTINUITY = "re-attach"
                return page["id"], page.get("url")
    LAST_CONTINUITY = "dropped:stale-id"
    _log_continuity("stale-id")
    return None, None


def _get_targets():
    return cdp("Target.getTargets").get("targetInfos") or []


def _attach(target_id):
    return cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]


def _json_pages():
    with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port()}/json/list", timeout=5) as resp:
        return json.loads(resp.read())


def _unique_url(url):
    """Distinct new-tab URL. A fragment, so the site never sees a changed request."""
    sep = "&" if "#" in url else "#"
    return f"{url}{sep}jev={time.time_ns()}"


def _wait_new_page(before_ids, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for page in _json_pages():
            if page.get("type") == "page" and page.get("id") not in before_ids:
                return page["id"]
        time.sleep(0.05)
    return None


def _open_via_new_tab(url):
    url = _unique_url(url)
    data = list_browsers()
    key = _pick_browser_key(data)
    before_json = {page.get("id") for page in _json_pages()}
    try:
        subprocess.run(
            [TB, "new-tab", "--browser", key, url],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise RuntimeError(
            "terminal-browser new-tab failed; Target.createTarget is not supported on this Electron. "
            f"{detail}"
        ) from exc
    created = _wait_new_page(before_json)
    if not created:
        raise RuntimeError("new-tab did not appear in CDP /json/list")
    return created, True


def _open_via_chrome(url):
    url = _unique_url(url)
    try:
        created = cdp("Target.createTarget", url=url, background=True)["targetId"]
        return created, True
    except RuntimeError as exc:
        raise RuntimeError(
            "Target.createTarget is not supported on this browser; "
            "wwwdrive only provisions terminal-browser panes (TUI-only scope)."
        ) from exc


def _open_owned_tab(url):
    last = _discover.LAST
    source = last.source if last else "terminal-browser"
    if source == "terminal-browser":
        return _open_via_new_tab(url)
    return _open_via_chrome(url)


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
        self._fresh_reason = "ok" if self.evaluate(MARKER) == page["marker"] else "target_changed"
        return self._fresh_reason == "ok"

    def _probe_target(self, kind, page, node, *, reprobe=True) -> str:
        """Probe one target until it is actionable, so a re-parented node is not a stale page.

        Same read-only probe every round: no re-snapshot, no action-list rebuild, no new page
        to decide from. Returns one of PROBE_REASONS.

        `target_detached` is final. snapshot.js hands ids out from a per-document counter and only
        prunes disconnected nodes on the next snapshot, which this decision will not take: a node
        that reads detached cannot re-attach inside it. Waiting out the full settle window would
        spend 0.6s to learn the same thing. Every other reason can still turn into `ok`, so it is
        retried.

        `reprobe=False` skips the settle window for a caller that just re-read the page and is
        about to retry regardless; the first probe still runs, so the verdict is unchanged.
        """
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
        """Execute one action, gating it on the freshness probe first.

        ``reprobe=False`` runs the probe once instead of retrying it. For a caller
        that has *already* re-read the page and is retrying anyway
        (`_retry_click`): that path re-observes immediately before acting, so the
        four-probe settle window has nothing left to wait for, and running it twice
        per click spent ~0.6s of duplicated wall clock to learn the same thing twice.
        The verdict is identical — same probe, same expression, same reason.
        """
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
        # Never Target.closeTarget on a TUI tab. Destroying Electron webContents
        # while ViewRegistry still holds the PageHost raises
        # "TypeError: Object has been destroyed" in PageHost.blurContent.
        if self.session:
            try:
                cdp("Target.detachFromTarget", sessionId=self.session)
            except RuntimeError:
                pass
        self.target = None
        self.session = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def _same_field_document(page: dict, page_key) -> bool:
    """A fill survives a re-render of the same URL; it does not survive leaving that URL.

    Deliberately not `_same_document`: a search box must survive a same-URL document swap.
    """
    if not isinstance(page_key, list) or len(page_key) < 2:
        return False
    stored = page.get("page_key")
    if not isinstance(stored, list) or len(stored) < 2:
        return False
    return page_key[1] == stored[1]


def _swapped_document(page: dict, page_key) -> bool:
    """Whether `page_key` comes from a different document that kept the same URL."""
    if not isinstance(page_key, list) or len(page_key) < 2:
        return False
    stored = page.get("page_key")
    if not isinstance(stored, list) or len(stored) < 2:
        return False
    return page_key[0] != stored[0]


def _same_field(page: dict, node: int, current) -> bool:
    """A fill is still valid when this field's identity, value, and URL are unchanged.

    The feed around a search box changes constantly. That must not cancel typing.
    """
    if not isinstance(current, list) or len(current) < 2:
        return False
    page_key, guard = current[0], current[1]
    stored_guard = (page.get("guards") or {}).get(str(node))
    if not _same_field_document(page, page_key):
        return False
    if not isinstance(guard, list) or not isinstance(stored_guard, list):
        return False
    if len(guard) < 4 or len(stored_guard) < 4:
        return False
    if guard[0] != stored_guard[0] or guard[3] != stored_guard[3]:
        return False
    if _swapped_document(page, page_key):
        # Across a document swap an id match is a collision, not an identity: ids come from a
        # per-document counter (snapshot.js `next:1`), so the new document hands id 4 to whatever
        # it observed fourth. Two empty fields at id 4 in one URL are indistinguishable, so the
        # value has to carry the match on its own: non-empty and equal. A field with no value to
        # match (empty, or contenteditable, whose guard carries none) is refused and re-observed
        # instead of being typed into blind — refusing costs a re-read, guessing costs the page.
        return bool(guard[3]) and bool(stored_guard[3])
    return True


_COUNTS = re.compile(r"\d[\d.,]*\s*[KMBkmb]?")


def without_counts(value):
    """Live feeds tick like counts and relative times. Those must not cancel a click."""
    return _COUNTS.sub("#", value) if isinstance(value, str) else value


def _same_document(page: dict, current) -> bool:
    stored = page.get("page_key")
    if not isinstance(current, list) or len(current) < 2:
        return False
    if not isinstance(stored, list) or len(stored) < 2:
        return current[1] == page.get("url")
    return current[0] == stored[0] and current[1] == stored[1]


def _same_target(page: dict, node: int, current) -> bool:
    """Same document and the same control. Scroll position and ticking numbers are ignored."""
    if not isinstance(current, list) or len(current) < 2:
        return False
    page_key, guard = current[0], current[1]
    if not _same_document(page, page_key):
        return False
    stored = (page.get("guards") or {}).get(str(node))
    if not isinstance(guard, list) or not isinstance(stored, list) or len(guard) != len(stored):
        return False
    return [without_counts(item) for item in guard] == [without_counts(item) for item in stored]


def _probe_expression(node: int) -> str:
    """The read-only probe for one observed node.

    Returns ``[pageKey, guard, [attached, live, inView, hit, writable]]``, or null when the
    document that produced the observation is gone. The bits are observations, not policy:
    `guard` is null unless the node is connected and visible, which is why `attached` and
    `live` travel beside it, and `_probe_reason` decides what they mean.

    `attached` connected; `live` also enabled (not :disabled, aria-disabled, or inert) and
    visible; `inView` its centre is inside the viewport; `hit` it has a box to point at and
    elementFromPoint at that centre does not return a stranger, so an overlay covering it is
    visible to the probe. An off-screen centre is not covered — the executor scrolls the node
    into view and re-hit-tests, and `page_key` ignores scroll, so gating on `inView` here
    would refuse every element below the fold that the executor lands fine.

    A fourth element carries telemetry: `enabled`, `visible`, `visiblePlain`,
    `opacity`, `boxed` — the halves of `live`, recorded separately so a
    `not_actionable` that never reaches the hit-test can say *which* half went
    false. `live` folds enabled-ness and opacity-sensitive visibility into one bit,
    and the opacity half is the one the executor's own gate does not test. Read by
    the telemetry writer and by nothing that decides; the verdict is computed from
    the five bits alone, exactly as before this field existed.
    """
    return (
        "(() => { const c=window.__jevFast, e=c?c.nodes.get("
        f"{node}"
        "):null; if (!c) return null; "
        "const g=c.guard(e), attached=!!(e&&e.isConnected), "
        "r=attached?e.getBoundingClientRect():{x:0,y:0,width:0,height:0}, "
        "x=r.x+r.width/2, y=r.y+r.height/2, "
        "boxed=!!(r.width&&r.height), "
        "inView=boxed&&x>=0&&y>=0&&x<innerWidth&&y<innerHeight, "
        "hit=boxed&&(!inView||e.contains(document.elementFromPoint(x,y))), "
        "live=attached&&!e.matches(':disabled')&&!e.closest('[aria-disabled=\"true\"],[inert]')"
        "&&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}), "
        "writable=live&&!e.readOnly&&e.getAttribute('aria-readonly')!=='true'; "
        "return [c.pageKey(),g,[attached,live,inView,hit,writable],"
        "{enabled:attached&&!e.matches(':disabled')&&!e.closest('[aria-disabled=\"true\"],[inert]'),"
        "visible:attached?e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}):false,"
        "visiblePlain:attached?e.checkVisibility({}):false,"
        "opacity:attached&&typeof getComputedStyle==='function'?getComputedStyle(e).opacity:null,boxed:boxed}]; })()"
    )


def _probe_flags(current) -> tuple | None:
    """(attached, live, inView, hit, writable), or None when the probe answered nothing usable.

    `inView` is telemetry: the verdict below does not read it, because the executor scrolls a
    node into view before it hit-tests and `page_key` ignores scroll.
    """
    if not isinstance(current, list) or len(current) < 3:
        return None
    bits = current[2]
    if not isinstance(bits, list) or len(bits) < 5:
        return None
    if any(bit is not True and bit is not False for bit in bits[:5]):
        return None
    return tuple(bits[:5])


def _probe_telemetry(current) -> dict:
    """The probe's diagnostic half, or {} when the answer carried none.

    Diagnostic only, and deliberately total: a missing or malformed fourth element
    must never change a verdict, so an old-shaped probe answer simply reports
    nothing rather than raising here.
    """
    if not isinstance(current, list) or len(current) < 4:
        return {}
    extra = current[3]
    return extra if isinstance(extra, dict) else {}


def _probe_reason(kind: str, page: dict, node: int, current) -> str:
    """One word for why this decision can no longer touch `node`.

    target_detached  the node left the document
    target_changed   another document, or a different control in this one
    not_actionable   disabled, hidden, boxless, or covered at its centre
    not_writable     a fill target that refuses text
    ok               this decision may still be executed

    Off-screen is not in that list. The executor scrollIntoViews an off-screen node and
    re-hit-tests it (see browser_operation), and a self-scrolling feed moves nodes under a
    decision without changing anything about them, so `hit` alone decides a pointer target.

    Identity is checked last for the target kinds: a control that is covered or disabled is
    still the control the model chose, and saying so beats reporting its state as a change.

    A pointer target is refused by `_clickable`, which reads `hit` as decisive and lets
    `live` refuse only what its telemetry can attribute to enabled-ness or a missing box.
    """
    flags = _probe_flags(current)
    if flags is None:
        return "target_changed"  # the document that produced the observation is gone
    page_key = current[0]
    if kind == "fill":
        if not _same_field_document(page, page_key):
            return "target_changed"
    elif not _same_document(page, page_key):
        return "target_changed"
    attached, live, _in_view, hit, writable = flags
    if not attached:
        return "target_detached"
    if kind == "fill":
        # Typing needs a field, not a pointer: a field covered by its own label is focused and
        # typed into (see _focus_covered_field), so the hit-test does not gate a fill.
        if not live:
            return "not_actionable"
        if not writable:
            return "not_writable"
    elif not _clickable(live, hit, _probe_telemetry(current)):
        return "not_actionable"
    same = _same_field(page, node, current) if kind == "fill" else _same_target(page, node, current)
    return "ok" if same else "target_changed"


def _clickable(live: bool, hit: bool, telemetry: dict) -> bool:
    """Whether a pointer target may still be clicked, given the probe's two bits.

    `hit` is the decisive one and always has been: it is a real hit-test at the
    node's centre. `live` folds enabled-ness together with an opacity-sensitive
    visibility check, and the two halves disagree exactly when a node is briefly
    mid-repaint — which is the shape behind the 14 `not_actionable` runs recorded
    against example.com, a link whose box and hit-test were clean throughout.

    So `live` refuses only what it can attribute:

    - `enabled` false — genuinely disabled, inert, or aria-disabled. Nothing
      recovers that, and refusing is right.
    - `boxed` false — no box, so there is nothing to point at and `hit` would be
      false anyway. Kept explicit because it is the attribution that matters.

    Anything else (a false `live` whose telemetry says enabled and boxed, i.e. a
    visibility blip) is deferred to the executor, which scrollIntoViews the node
    and re-hit-tests it before dispatching anything (browser_operation). That gate
    is authoritative because it is the one about to send input, and it is strictly
    more capable than this one. With no telemetry at all the old rule stands, so a
    probe answer that predates the diagnostic is judged exactly as it was.
    """
    if not telemetry:
        return live and hit
    if not telemetry.get("enabled", live):
        return False
    if not telemetry.get("boxed", True):
        return False
    return hit


def _offer_enter(page: dict | None) -> None:
    """Press Enter is its own action once a field holds text. Jev does not imply it."""
    if not isinstance(page, dict):
        return
    actions = page.get("actions")
    if not isinstance(actions, list):
        return
    if any(item.get("id") == "press_enter" for item in actions):
        return
    if any(item.get("kind") == "fill" and str(item.get("value") or "").strip() for item in actions):
        actions.append({"id": "press_enter", "kind": "enter", "label": "Press Enter"})


def _insert_fill(call, text: str) -> None:
    modifier = 4 if sys.platform == "darwin" else 2
    call(
        "Input.dispatchKeyEvent",
        type="keyDown",
        key="a",
        code="KeyA",
        modifiers=modifier,
        commands=["selectAll"],
    )
    call(
        "Input.dispatchKeyEvent",
        type="keyUp",
        key="a",
        code="KeyA",
        modifiers=modifier,
    )
    call("Input.insertText", text=text)


def _focus_covered_field(evaluate, action) -> bool:
    """Focus a fill target the hit-test could not click. The field is often covered by its own label."""
    try:
        focused = evaluate(
            """(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]')) return null;
              if (e.readOnly || e.getAttribute('aria-readonly')==='true') return null;
              e.focus();
              return true;
            })("""
            + json.dumps(action)
            + ")"
        )
    except StalePage:
        return False
    return focused is True


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "enter":
            for event in ("keyDown", "keyUp"):
                call(
                    "Input.dispatchKeyEvent",
                    type=event,
                    key="Enter",
                    code="Enter",
                    windowsVirtualKeyCode=13,
                    nativeVirtualKeyCode=13,
                )
            return {"executed": action["id"], "via": "enter"}
        if kind == "scroll":
            size = evaluate("({h: innerHeight, w: innerWidth})") or {}
            height = size.get("h") or 700
            width = size.get("w") or 1100
            sign = 1 if (action.get("delta") or 0) > 0 else -1
            delta = sign * int(height * 0.8)
            x, y = width / 2, height / 2
            call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=0, deltaY=delta)
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              let x=0, y=0;
              const hit=()=>{
                const r=e.getBoundingClientRect();
                x=r.x+r.width/2; y=r.y+r.height/2;
                return r.width && r.height && x>=0 && y>=0 && x<innerWidth && y<innerHeight &&
                  e.contains(document.elementFromPoint(x,y));
              };
              if (!hit()) {
                e.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});
                if (!hit()) return null;
              }
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                if kind == "fill" and _focus_covered_field(evaluate, action):
                    _insert_fill(call, request.get("text") or "")
                    return {"executed": action["id"], "via": "focus"}
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    _insert_fill(call, request.get("text") or "")
            via = "pointer"
        else:
            via = "wait"
        if kind == "scroll":
            via = "wheel"
        return {"executed": action["id"], "via": via}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
