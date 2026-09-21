"""Observed actions through terminal-browser CDP; one session, no per-step subprocess."""

import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import discover as _discover
from .cdp import TB, cdp, cdp_port, connect, list_browsers

READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

BLOCKED_SCHEMES = ("chrome:", "chrome-untrusted:", "devtools:", "chrome-extension:")
DENYLIST_HOSTS = ("hindsight.vectorize.io",)

LEASE = {
    "tab": "new",
    "target_id": None,
    "browser_key": None,
    "navigate": True,
    "create_fallback": None,
}

LAST_PAGE_PATH = Path.home() / ".cache" / "jev-driver" / "last-page.json"
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
    print(f"jev-driver: continuity {reason}", file=sys.stderr)


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


def remember_page(target_id, url):
    if not target_id or _is_ephemeral_url(url):
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
    if (remembered.get("source"), remembered.get("browser_id")) != (source, browser_id):
        LAST_CONTINUITY = "dropped:browser-mismatch"
        _log_continuity("browser-mismatch")
        return None, None
    try:
        pages = _json_pages()
    except (OSError, json.JSONDecodeError, TimeoutError):
        pages = []
    by_id = {p.get("id"): p for p in pages if p.get("type") == "page" and p.get("id")}
    remembered_id = remembered.get("targetId")
    if remembered_id and remembered_id in by_id:
        info = by_id[remembered_id]
        url = info.get("url") or ""
        if not _is_ephemeral_url(url) and _target_ok({"type": "page", "url": url}, allow_denylist=False):
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


def _tui_target_ids(data=None):
    data = data or list_browsers()
    return {t["targetId"] for b in data.get("browsers") or [] for t in b.get("tabs") or []}


def _json_pages():
    with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port()}/json/list", timeout=5) as resp:
        return json.loads(resp.read())


def _unique_url(url):
    sep = "&" if "?" in url else "?"
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
    before_json = {page.get("id") for page in _json_pages()}
    try:
        created = cdp("Target.createTarget", url=url, background=True)["targetId"]
        return created, True
    except RuntimeError:
        binary = _discover.resolve_agent_browser()
        if not binary:
            raise
        session = (_discover.LAST.session if _discover.LAST else None) or _discover.SESSION
        subprocess.run(
            _discover.agent_browser_argv(binary) + ["--session", session, "open", url],
            check=False,
            timeout=60,
            capture_output=True,
            text=True,
        )
        created = _wait_new_page(before_json)
        if not created:
            raise RuntimeError("agent-browser open did not produce a CDP page")
        return created, True


def _open_owned_tab(url):
    last = _discover.LAST
    source = last.source if last else "terminal-browser"
    if source == "terminal-browser":
        return _open_via_new_tab(url)
    return _open_via_chrome(url)


class Browser:
    def __init__(self, url):
        connect()
        self.owned = False
        self.target = None
        self.session = None
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
                self.call("Page.navigate", url=url)
        else:
            self.target, self.owned = _open_owned_tab(url)
            self.session = _attach(self.target)
            if LEASE["create_fallback"] and LEASE["navigate"]:
                self.call("Page.navigate", url=url)
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

    def observe(self, screenshot=True):
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
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
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
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
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
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
