"""Tab lease, last-page memory, and the tab-open helpers that provision an owned tab."""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import discover as _discover
from .cdp import TB, cdp, cdp_port, list_browsers
from .runlog import write_event

BLOCKED_SCHEMES = ("chrome:", "chrome-untrusted:", "devtools:", "chrome-extension:")
DENYLIST_HOSTS = ("hindsight.vectorize.io",)

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
# `auto_launched` is optional on disk: LAST_PAGE_KEYS is a presence test, so adding
# it would void old records. Absent means unknown provenance, not False.
PROVENANCE_KEY = "auto_launched"
LAST_CONTINUITY = None


def set_lease(*, tab="new", target_id=None, browser_key=None, navigate=True):
    LEASE.update(tab=tab, target_id=target_id, browser_key=browser_key, navigate=navigate, create_fallback=None)


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
    """Remember the driver's tab for reuse; preserve ``auto_launched`` on re-attach."""
    if not target_id:
        return
    source, browser_id = browser_identity()
    spawned = bool(getattr(_discover.LAST, "auto_launched", False))
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
            PROVENANCE_KEY: spawned,
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
