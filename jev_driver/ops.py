"""Enter-offer policy and the CDP-level read/act executor.

``browser_operation`` is the one function that actually dispatches input or
re-reads the page; `Browser` calls it through the `browser` module namespace so
it stays patchable from there. `StalePage` is imported lazily (at call time, not
at module import time) to avoid a cycle with `browser`, which imports this module.
"""

import json
import sys
from pathlib import Path

from .cdp import cdp
from .probe import fingerprint

# Duplicated from browser.py rather than imported: both modules read the same
# file by name, and importing it from `browser` at module scope would cycle
# (`browser` imports this module to re-export `browser_operation`).
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()


def _enter_is_read_only_submit(action: dict) -> bool:
    """True when Enter is a search-only submit (``searchbox`` role — narrow on purpose)."""
    return (action.get("role") or "") == "searchbox"


def _offer_enter(page: dict | None) -> None:
    """Offer Enter when the field holds text or is a searchbox — never implied by Jev."""
    if not isinstance(page, dict):
        return
    actions = page.get("actions")
    if not isinstance(actions, list):
        return
    if any(item.get("id") == "press_enter" for item in actions):
        return
    if any(
        item.get("kind") == "fill"
        and (str(item.get("value") or "").strip() or _enter_is_read_only_submit(item))
        for item in actions
    ):
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
    from .browser import StalePage

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
    from .browser import StalePage

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
