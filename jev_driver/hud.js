/* Jev debug overlay. Draws into its own fixed layer; never restyles page elements. */
window.__jevHudPaint = function (payload) {
  payload = payload || {};
  if (typeof window.__jevHudOpen !== "boolean") window.__jevHudOpen = payload.open === true;
  const open = window.__jevHudOpen;
  try {
    sessionStorage.setItem("__jevHud", JSON.stringify({ ...payload, marks: [], open }));
  } catch (e) {}

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const pct = (n) => (n == null || isNaN(Number(n)) ? "" : Math.round(Number(n) * 100) + "%");
  const ICON = {
    CLICK: "↖", TYPE_TEXT: "✎", SELECT: "☰", PRESS_ENTER: "⏎", SCROLL_DOWN: "↓", SCROLL_UP: "↑",
    WAIT: "…", DONE: "✓", BLOCKED: "■", click: "↖", fill: "✎", select: "☰", enter: "⏎", scroll: "↕", wait: "…",
  };
  const status = payload.status || "ready";
  const tone = status === "done" ? "#4ade80" : status === "blocked" ? "#fbbf24" : "#60a5fa";

  let root = document.getElementById("jev-debug-hud");
  if (!root) {
    root = document.createElement("div");
    root.id = "jev-debug-hud";
    root.setAttribute("data-jev-hud", "1");
    root.setAttribute("aria-hidden", "true");
    root.style.cssText =
      "position:fixed;inset:0;z-index:2147483647;pointer-events:none;" +
      "font:12px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif;color:#e8edf2;letter-spacing:.01em";
    root.innerHTML =
      "<style>" +
      "#jev-debug-hud .jr{position:fixed;border-radius:8px;transition:all .12s ease-out;box-sizing:border-box}" +
      "#jev-debug-hud .jr.c{border:2px solid #4ade80;box-shadow:0 0 0 4px rgba(74,222,128,.18),0 0 18px rgba(74,222,128,.35)}" +
      "#jev-debug-hud .jr.a{border:1.5px dashed rgba(148,163,184,.75)}" +
      "#jev-debug-hud .jp{position:absolute;left:-2px;top:-24px;white-space:nowrap;max-width:320px;overflow:hidden;" +
      "text-overflow:ellipsis;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}" +
      "#jev-debug-hud .jr.c .jp{background:#4ade80;color:#052e16}" +
      "#jev-debug-hud .jb{position:absolute;right:-8px;top:-8px;min-width:16px;height:16px;padding:0 4px;border-radius:999px;" +
      "background:rgba(30,41,59,.92);border:1px solid rgba(148,163,184,.6);font-size:10px;line-height:15px;text-align:center}" +
      "#jev-debug-hud .jpanel{position:fixed;right:14px;bottom:14px;pointer-events:auto;border-radius:14px;overflow:hidden;" +
      "background:rgba(15,19,25,.66);backdrop-filter:blur(18px) saturate(140%);-webkit-backdrop-filter:blur(18px) saturate(140%);" +
      "border:1px solid rgba(255,255,255,.12);box-shadow:0 12px 36px rgba(0,0,0,.38)}" +
      "#jev-debug-hud .jh{display:flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer;user-select:none}" +
      "#jev-debug-hud .jdot{width:8px;height:8px;border-radius:50%;flex:none}" +
      "#jev-debug-hud .jk{font-size:10px;text-transform:uppercase;letter-spacing:.08em;opacity:.55;margin:10px 0 4px}" +
      "#jev-debug-hud .jbody{padding:0 12px 12px;max-height:52vh;overflow:auto}" +
      "#jev-debug-hud .jbar{height:4px;border-radius:2px;background:rgba(255,255,255,.1);overflow:hidden;margin-top:2px}" +
      "#jev-debug-hud .jbar>i{display:block;height:100%}" +
      "#jev-debug-hud .jstep{display:grid;grid-template-columns:18px 16px 1fr auto;gap:6px;align-items:baseline;padding:2px 0}" +
      "#jev-debug-hud .jstep.now{color:#fff;font-weight:600}" +
      "#jev-debug-hud .jmut{opacity:.55}" +
      "</style><div data-jev-hud-rings></div><div data-jev-hud-slot></div>";
    document.documentElement.appendChild(root);
  }

  const nodes = (window.__jevFast && window.__jevFast.nodes) || new Map();
  const marks = (payload.marks || []).filter((m) => m.chosen || Number(m.p) >= 0.05);
  const chosen = marks.filter((m) => m.chosen);
  const others = marks.filter((m) => !m.chosen).slice(0, 3);
  const ringsHost = root.querySelector("[data-jev-hud-rings]");
  const layout = () => {
    let html = "";
    let rank = 1;
    for (const mark of [...chosen, ...others]) {
      const node = nodes.get(mark.node);
      if (!node || !node.isConnected) continue;
      const r = node.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0 || r.bottom < 0 || r.top > innerHeight) continue;
      const box = `left:${r.left - 4}px;top:${r.top - 4}px;width:${r.width + 8}px;height:${r.height + 8}px`;
      if (mark.chosen) {
        const pill = `${ICON[payload.operation] || ""} ${payload.operation || ""} ${pct(mark.p)} · ${mark.label || ""}`;
        html += `<div class="jr c" style="${box}"><div class="jp">${esc(pill)}</div></div>`;
      } else {
        rank += 1;
        html += `<div class="jr a" style="${box}" title="${esc(mark.label)}"><div class="jb">${rank} · ${esc(pct(mark.p))}</div></div>`;
      }
    }
    ringsHost.innerHTML = html;
  };
  layout();
  if (window.__jevHudLayout) {
    removeEventListener("scroll", window.__jevHudLayout, true);
    removeEventListener("resize", window.__jevHudLayout);
  }
  let pending = false;
  window.__jevHudLayout = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      layout();
    });
  };
  addEventListener("scroll", window.__jevHudLayout, { capture: true, passive: true });
  addEventListener("resize", window.__jevHudLayout, { passive: true });

  const op = payload.operation || (status === "ready" ? "thinking" : status);
  const headline = `${ICON[payload.operation] || ""} ${op}${payload.target_label ? " · " + payload.target_label : ""}`;
  const stepCount = payload.step || 0;
  const header =
    `<div class="jh" data-jev-hud-toggle="1"><span class="jdot" style="background:${tone};box-shadow:0 0 8px ${tone}"></span>` +
    `<b style="font-weight:700">Jev</b><span class="jmut">step ${esc(stepCount)}</span>` +
    `<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(headline)}</span>` +
    `<span class="jmut">${pct(payload.confidence)}</span><span class="jmut">${open ? "▾" : "▴"}</span></div>`;

  let body = "";
  if (open) {
    const bar = (p, color) =>
      `<div class="jbar"><i style="width:${Math.max(2, Math.round(Number(p || 0) * 100))}%;background:${color}"></i></div>`;
    const steps = (payload.steps || [])
      .map((s) => {
        const mark = s.changed === true ? "✓" : s.changed === false ? "·" : "";
        return (
          `<div class="jstep"><span class="jmut">${esc(s.n)}</span><span>${ICON[s.op] || "•"}</span>` +
          `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(s.label)}">${esc(s.label || s.op)}` +
          `${s.text ? ' <span class="jmut">“' + esc(s.text) + "”</span>" : ""}</span>` +
          `<span class="jmut" title="page changed">${mark}</span></div>`
        );
      })
      .join("");
    const nextRow =
      status === "done" || status === "blocked"
        ? `<div class="jstep now"><span></span><span style="color:${tone}">${status === "done" ? "✓" : "■"}</span><span>${esc(status)}</span><span></span></div>`
        : payload.operation
          ? `<div class="jstep now"><span class="jmut">${esc(stepCount + 1)}</span><span>${ICON[payload.operation] || "•"}</span>` +
            `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(payload.target_label || payload.operation)}</span>` +
            `<span class="jmut">next</span></div>`
          : "";
    const targets = (payload.targets || [])
      .filter((row) => Number(row[1]) >= 0.01 || row[0] === payload.target_label)
      .slice(0, 5)
      .map((row) => {
        const isChosen = payload.target_label && row[0] === payload.target_label;
        return (
          `<div style="margin-bottom:4px"><div style="display:flex;justify-content:space-between;gap:8px">` +
          `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${isChosen ? "● " : ""}${esc(row[0])}</span>` +
          `<span class="jmut">${pct(row[1])}</span></div>${bar(row[1], isChosen ? "#4ade80" : "rgba(148,163,184,.7)")}</div>`
        );
      })
      .join("");
    const ops = (payload.ops || [])
      .filter((row, i) => i === 0 || Number(row[1]) >= 0.01)
      .slice(0, 4)
      .map(
        (row) =>
          `<div style="display:flex;align-items:center;gap:6px"><span style="width:92px">${esc(row[0])}</span>` +
          `<span style="flex:1">${bar(row[1], "#60a5fa")}</span><span class="jmut" style="width:34px;text-align:right">${pct(row[1])}</span></div>`,
      )
      .join("");
    const usage = payload.usage || {};
    const spend =
      usage.input_tokens || usage.output_tokens
        ? `${usage.input_tokens || 0} in · ${usage.output_tokens || 0} out${usage.cost ? " · $" + Number(usage.cost).toFixed(4) : ""}`
        : "";
    body =
      `<div class="jbody">` +
      `<div class="jk">Goal</div><div>${esc(payload.goal)}</div>` +
      (payload.degenerate ? `<div style="color:#fbbf24;margin-top:6px">Split decision: the top two operations are close.</div>` : "") +
      (payload.why ? `<div class="jk">Why</div><div>${esc(payload.why)}</div>` : "") +
      (payload.reason ? `<div class="jk">Stopped</div><div style="color:${tone}">${esc(payload.reason)}</div>` : "") +
      `<div class="jk">Steps</div>${steps || ""}${nextRow || (steps ? "" : '<div class="jmut">none yet</div>')}` +
      (targets ? `<div class="jk">Candidates</div>${targets}` : "") +
      (ops ? `<div class="jk">Operation</div>${ops}` : "") +
      (spend ? `<div class="jk">Spend</div><div class="jmut">${esc(spend)}</div>` : "") +
      (payload.url ? `<div class="jmut" style="margin-top:10px;font-size:10px;word-break:break-all">${esc(payload.url)}</div>` : "") +
      `</div>`;
  }
  const slot = root.querySelector("[data-jev-hud-slot]");
  slot.innerHTML = `<div class="jpanel" style="width:${open ? "340px" : "auto"};max-width:calc(100vw - 28px)">${header}${body}</div>`;
  const toggle = slot.querySelector("[data-jev-hud-toggle]");
  toggle.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    window.__jevHudOpen = !open;
    window.__jevHudPaint({ ...payload, open: window.__jevHudOpen });
  };
  return open;
};

window.__jevHudRestore = function () {
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem("__jevHud") || "null");
  } catch (e) {}
  return saved ? window.__jevHudPaint(saved) : null;
};
