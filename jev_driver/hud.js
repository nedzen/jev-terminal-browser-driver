window.__jevHudPaint = function (payload) {
  payload = payload || {};
  let root = document.getElementById("jev-debug-hud");
  if (!root) {
    root = document.createElement("div");
    root.id = "jev-debug-hud";
    root.setAttribute("data-jev-hud", "1");
    root.setAttribute("aria-hidden", "true");
    root.setAttribute("inert", "");
    root.style.cssText =
      "position:fixed;inset:0;z-index:2147483647;pointer-events:none;font:12px/1.4 ui-monospace,monospace;color:#fff";
    document.documentElement.appendChild(root);
  }
  const bar = payload.goal
    ? (payload.operation || "?") +
      " p=" +
      (payload.confidence != null ? Number(payload.confidence).toFixed(2) : "?") +
      (payload.target ? " " + payload.target : "") +
      " — " +
      String(payload.goal).slice(0, 160)
    : "(observing)";
  const ops = payload.ops || [];
  const targets = payload.targets || [];
  const lines = [];
  lines.push("ops: " + ops.map(function (o) { return o[0] + " " + Number(o[1]).toFixed(2); }).join(" | "));
  lines.push("targets: " + targets.map(function (o) { return o[0] + " " + Number(o[1]).toFixed(2); }).join(" | "));
  root.innerHTML =
    '<div style="position:absolute;top:0;left:0;right:0;padding:6px 10px;background:rgba(180,0,0,.85)">' +
    bar.replace(/</g, "&lt;") +
    '</div><pre style="position:absolute;bottom:0;left:0;right:0;margin:0;padding:8px 10px;background:rgba(0,0,0,.8);white-space:pre-wrap">' +
    lines.join("\n").replace(/</g, "&lt;") +
    "</pre>";
  const nodes = (window.__jevFast && window.__jevFast.nodes) || new Map();
  for (const el of nodes.values()) {
    if (el && el.style) el.style.outline = "2px solid red";
  }
};
