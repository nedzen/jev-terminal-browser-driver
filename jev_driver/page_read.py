"""In-page outline and caller script. Not the Jev click loop."""

from __future__ import annotations

import json

MAX_SCROLLS = 15
MAX_RESULT = 80_000

OUTLINE_JS = r"""
(() => {
  const skip = (el) => !!el.closest('[aria-hidden="true"],[data-jev-hud]');
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const textOf = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 180);
  const rows = [];
  for (const el of document.querySelectorAll('article, [role="article"], h1, h2, h3, time, a[href]')) {
    if (skip(el) || !visible(el)) continue;
    const text = textOf(el);
    if (!text) continue;
    const link = el.tagName === 'A' ? el.href : (el.querySelector('a[href]') || {}).href || '';
    rows.push({ tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', text, href: link || '' });
    if (rows.length >= 60) break;
  }
  return { url: location.href, title: document.title, outline: rows };
})()
"""


def clamp_scrolls(value) -> int:
    try:
        scrolls = int(value or 0)
    except (TypeError, ValueError):
        scrolls = 0
    return max(0, min(scrolls, MAX_SCROLLS))


def read_expression(script: str | None, scrolls: int) -> str:
    """Async page function. Scrolls first, then either the outline or the caller body."""
    n = clamp_scrolls(scrolls)
    scroll = (
        f"for (let i = 0; i < {n}; i++) {{"
        " window.scrollBy(0, Math.round((window.innerHeight || 800) * 0.85));"
        " await new Promise((r) => setTimeout(r, 350)); }"
    )
    body = (script or "").strip()
    if not body:
        return f"(async () => {{ {scroll} return {OUTLINE_JS}; }})()"
    return (
        f"(async () => {{ {scroll} const value = await (async () => {{ {body}\\n }})();"
        f" const text = JSON.stringify(value);"
        f" if (text == null) return {{ error: 'script returned undefined' }};"
        f" if (text.length > {MAX_RESULT}) return {{ truncated: true, json: text.slice(0, {MAX_RESULT}) }};"
        f" return {{ json: text }}; }})()"
    )


def evaluate_async(browser, expression: str):
    response = browser.call(
        "Runtime.evaluate",
        expression=expression,
        returnByValue=True,
        awaitPromise=True,
    )
    details = response.get("exceptionDetails") or {}
    if details:
        text = ((details.get("exception") or {}).get("description")) or details.get("text") or "script failed"
        return {"error": str(text)[:500]}
    return response.get("result", {}).get("value")


def read_page(browser, script: str | None, scrolls: int) -> dict:
    value = evaluate_async(browser, read_expression(script, scrolls))
    url = ""
    try:
        url = browser.evaluate("location.href") or ""
    except Exception:
        url = ""
    if not isinstance(value, dict):
        return {"success": False, "url": url, "error": "page returned no object"}
    if value.get("error"):
        return {"success": False, "url": url, "error": value["error"]}
    if "outline" in value:
        return {
            "success": True,
            "url": value.get("url") or url,
            "title": value.get("title") or "",
            "outline": value.get("outline") or [],
        }
    raw = value.get("json")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {"success": False, "url": url, "error": "script result was not JSON"}
    out = {"success": True, "url": url, "data": data}
    if value.get("truncated"):
        out["truncated"] = True
    return out
