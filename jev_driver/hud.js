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
  function bar(p) {
    var w = Math.max(0, Math.min(100, Math.round(Number(p) * 100)));
    return (
      '<span style="display:inline-block;width:52px;height:6px;margin-right:6px;background:rgba(255,255,255,.12);vertical-align:middle">' +
      '<span style="display:block;height:100%;width:' + w + '%;background:#9dffc2"></span></span>'
    );
  }
  var root = document.getElementById("jev-debug-hud");
  if (!root) {
    root = document.createElement("div");
    root.id = "jev-debug-hud";
    root.setAttribute("data-jev-hud", "1");
    root.setAttribute("aria-hidden", "true");
    root.style.cssText =
      "position:fixed;inset:0;z-index:2147483647;pointer-events:none;font:12px/1.4 ui-monospace,monospace;color:#f4f7f5";
    document.documentElement.appendChild(root);
  }
  var nodes = (window.__jevFast && window.__jevFast.nodes) || new Map();
  for (var el of nodes.values()) {
    if (el && el.style) el.style.outline = "";
  }
  var marks = payload.marks || [];
  var boxes = [];
  for (var mark of marks) {
    var node = nodes.get(mark.node);
    if (!node || !node.getBoundingClientRect) continue;
    if (node.style) node.style.outline = mark.chosen ? "3px solid #3dff7a" : "2px solid rgba(255,70,70,.9)";
    var r = node.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var bg = mark.chosen ? "rgba(0,90,30,.92)" : "rgba(120,0,0,.88)";
    boxes.push(
      '<div style="position:absolute;left:' + Math.max(0, r.left) + "px;top:" + Math.max(0, r.top - 16) +
      "px;max-width:280px;padding:1px 4px;background:" + bg + ';pointer-events:none;white-space:nowrap;overflow:hidden">' +
      esc((mark.chosen ? "* " : "") + pct(mark.p) + " " + (mark.label || "")) + "</div>"
    );
  }
  var open = window.__jevHudOpen === true;
  var op = payload.operation || "…";
  var chip = esc(op + (payload.target_label ? " " + payload.target_label : "") + " " + pct(payload.confidence));
  var rows = "";
  (payload.ops || []).slice(0, 6).forEach(function (row) {
    rows += "<div>" + bar(row[1]) + esc(row[0]) + " " + pct(row[1]) + "</div>";
  });
  var hits = "";
  (payload.targets || []).forEach(function (row) {
    var chosen = payload.target_label && row[0] === payload.target_label;
    hits += "<div>" + bar(row[1]) + esc((chosen ? "* " : "") + row[0]) + " " + pct(row[1]) + "</div>";
  });
  var steps = "";
  (payload.steps || []).forEach(function (step) {
    var changed = step.changed === true ? "changed" : step.changed === false ? "same" : "";
    steps += "<div>" + esc((step.n || "") + " " + (step.op || "") + " " + (step.label || "") + " " + pct(step.p) + " " + changed) + "</div>";
  });
  var usage = payload.usage || {};
  var cost = usage.cost ? " $" + Number(usage.cost).toFixed(4) : "";
  var tokens = (usage.input_tokens || usage.output_tokens)
    ? esc((usage.input_tokens || 0) + " in / " + (usage.output_tokens || 0) + " out" + cost)
    : "";
  var warn = payload.degenerate ? '<div style="color:#ffb020">split decision — top two operations are close</div>' : "";
  var panel =
    '<div data-jev-hud-panel="1" style="position:absolute;right:12px;bottom:12px;width:' + (open ? "320px" : "auto") +
    ";max-width:min(320px,calc(100vw - 24px));pointer-events:auto;border-radius:12px;overflow:hidden;" +
    "background:rgba(12,16,20,.62);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);" +
    'box-shadow:0 8px 28px rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.14)">' +
    '<div data-jev-hud-toggle="1" style="padding:8px 10px;cursor:pointer">' +
    esc(open ? "▾" : "▸") + " Jev · " + chip + "</div>" +
    (open
      ? '<div style="padding:0 10px 10px;max-height:46vh;overflow:auto">' +
        warn +
        '<div style="opacity:.7;margin-top:4px">intent</div><div>' + esc(payload.goal || "") + "</div>" +
        '<div style="opacity:.7;margin-top:8px">why</div><div>' + esc(payload.why || payload.status || "") + "</div>" +
        (payload.reason ? '<div style="opacity:.7;margin-top:8px">stop</div><div>' + esc(payload.reason) + "</div>" : "") +
        '<div style="opacity:.7;margin-top:8px">operations</div>' + (rows || "<div>—</div>") +
        '<div style="opacity:.7;margin-top:8px">hits</div>' + (hits || "<div>—</div>") +
        '<div style="opacity:.7;margin-top:8px">steps ' + esc(payload.step || 0) + "</div>" + (steps || "<div>—</div>") +
        (tokens ? '<div style="opacity:.7;margin-top:8px">spend</div><div>' + tokens + "</div>" : "") +
        (payload.url ? '<div style="opacity:.55;margin-top:8px;word-break:break-all">' + esc(payload.url) + "</div>" : "") +
        "</div>"
      : "") +
    "</div>";
  root.innerHTML = boxes.join("") + panel;
  var toggle = root.querySelector("[data-jev-hud-toggle]");
  if (toggle) {
    toggle.onclick = function (event) {
      event.preventDefault();
      event.stopPropagation();
      window.__jevHudOpen = !open;
      window.__jevHudPaint(payload);
    };
  }
};
