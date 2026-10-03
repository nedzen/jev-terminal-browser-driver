"""Page/field fingerprinting and the read-only freshness probe.

``fingerprint()`` hashes an observed page for the decision wire. The ``_same_*``
family and the ``_probe_*`` helpers decide whether a previously observed node or
document is still the one `act()` is about to touch — ticking counts and the
driver's own `#jev=` nonce must not read as staleness.
"""

import hashlib
import json
import re

# Settle windows before a target counts as stale (re-probe is cheaper than re-snapshot).
PROBE_RETRIES = (0.1, 0.2, 0.3)
# Probe refusal reasons (telemetry; act() still reports field/page_changed).
PROBE_REASONS = ("target_detached", "target_changed", "not_actionable", "not_writable", "ok")

def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

def _same_field_document(page: dict, page_key) -> bool:
    """True when the fill's URL still matches (same-URL re-render is OK)."""
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
    """True when field identity, value, and URL are unchanged."""
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
        # After a document swap, id alone is a collision; require a non-empty equal value.
        return bool(guard[3]) and bool(stored_guard[3])
    return True

_COUNTS = re.compile(r"\d[\d.,]*\s*[KMBkmb]?")

def without_counts(value):
    """Live feeds tick like counts and relative times. Those must not cancel a click."""
    return _COUNTS.sub("#", value) if isinstance(value, str) else value

# Driver-authored fragment param from `_unique_url` (whole parameter only).
_JEV_MARKER_RE = re.compile(r"jev=\d+")

def _same_marker(stored, current) -> bool:
    """True when two MARKER readings match after stripping the driver nonce."""
    if not isinstance(stored, list) or not isinstance(current, list):
        return stored == current
    if len(stored) != len(current) or len(stored) < 2:
        return stored == current
    return _same_href(stored[1], current[1]) and stored[:1] == current[:1] and stored[2:] == current[2:]

def _strip_jev_marker(href):
    """URL with the driver's ``#jev=<nonce>`` fragment parameter removed."""
    if not isinstance(href, str):
        return href
    base, sep, fragment = href.partition("#")
    if not sep or not fragment:
        return href
    kept = [part for part in fragment.split("&") if not _JEV_MARKER_RE.fullmatch(part)]
    return f"{base}#{'&'.join(kept)}" if kept else base

def _same_href(stored, current) -> bool:
    """Two hrefs are the same place, ignoring the driver's own nonce fragment."""
    return _strip_jev_marker(stored) == _strip_jev_marker(current)

def _same_document(page: dict, current) -> bool:
    stored = page.get("page_key")
    if not isinstance(current, list) or len(current) < 2:
        return False
    if not isinstance(stored, list) or len(stored) < 2:
        return _same_href(current[1], page.get("url"))
    return current[0] == stored[0] and _same_href(current[1], stored[1])

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
    """Read-only probe expression for one node (bits + optional telemetry).

    ``inView`` is telemetry only — the executor scrolls before hit-testing.
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
    """Probe flag tuple, or None when the answer is unusable."""
    if not isinstance(current, list) or len(current) < 3:
        return None
    bits = current[2]
    if not isinstance(bits, list) or len(bits) < 5:
        return None
    if any(bit is not True and bit is not False for bit in bits[:5]):
        return None
    return tuple(bits[:5])

def _probe_telemetry(current) -> dict:
    """Probe telemetry dict, or {} — never affects the verdict."""
    if not isinstance(current, list) or len(current) < 4:
        return {}
    extra = current[3]
    return extra if isinstance(extra, dict) else {}

def _probe_reason(kind: str, page: dict, node: int, current) -> str:
    """One of PROBE_REASONS explaining why ``node`` cannot be touched."""
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
    """Whether a pointer target may still be clicked.

    ``hit`` is decisive. ``live`` refuses only attributable enabled/boxed failures;
    visibility blips defer to the executor's scroll + re-hit-test.
    """
    if not telemetry:
        return live and hit
    if not telemetry.get("enabled", live):
        return False
    if not telemetry.get("boxed", True):
        return False
    return hit
