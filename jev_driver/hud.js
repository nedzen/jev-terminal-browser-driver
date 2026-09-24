window.__jevHudPaint = function (payload) {
  payload = payload || {};
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>]/g, function (c) {
      return c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;";
    });
  }
  function pct(n) {
    var x = Number(n);
    return n == null || isNaN(x) ? "?" : x.toFixed(2);
  }
  let root = document.getElementById("jev-debug-hud");
  if (!root) {
    root = document.createElement("div");
    root.id = "jev-debug-hud";
    root.setAttribute("data-jev-hud", "1");
    root.setAttribute("aria-hidden", "true");
    root.setAttribute("inert", "");
    root.style.cssText =
      "position:fixed;inset:0;z-index:2147483647;pointer-events:none;font:12px/1.35 ui-monospace,monospace;color:#fff";
    document.documentElement.appendChild(root);
  }
  const nodes = (window.__jevFast && window.__jevFast.nodes) || new Map();
  for (const el of nodes.values()) {
    if (el && el.style) el.style.outline = "";
  }
  const marks = payload.marks || [];
  const boxes = [];
  for (const mark of marks) {
    const el = nodes.get(mark.node);
    if (!el || !el.getBoundingClientRect) continue;
    if (el.style) el.style.outline = mark.chosen ? "3px solid #3dff7a" : "2px solid rgba(255,70,70,.9)";
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const bg = mark.chosen ? "rgba(0,90,30,.92)" : "rgba(120,0,0,.88)";
    boxes.push(
      '<div style="position:absolute;left:' +
        Math.max(0, r.left) +
        "px;top:" +
        Math.max(0, r.top - 16) +
        "px;max-width:280px;padding:1px 4px;background:" +
        bg +
        ';white-space:nowrap;overflow:hidden">' +
        esc((mark.chosen ? "* " : "") + pct(mark.p) + " " + (mark.label || "")) +
        "</div>"
    );
  }
  root.innerHTML = boxes.join("");
};
