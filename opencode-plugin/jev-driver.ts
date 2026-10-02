/**
 * jev-driver plugin for OpenCode v2.
 *
 * Exposes jev_drive / jev_read as native tools by spawning the same CLI the
 * Hermes plugin uses (`uv run python scripts/drive.py --json`). The Hermes
 * adapter under plugin/ is untouched; tool schemas below are synced from
 * plugin/__init__.py (DESCRIPTION, PARAMETERS, READ_DESCRIPTION,
 * READ_PARAMETERS).
 *
 * Install: copy or symlink this file to ~/.config/opencode/plugins/jev-driver.ts
 * (global) or .opencode/plugins/jev-driver.ts (project). Requires `uv`, the
 * repo checkout at JEV_DRIVER_HOME (or auto-detected), `terminal-browser` on
 * PATH, and TYPESAFE_API_KEY in env. Debug overlay: plugin options
 * `{ debug: true }` in opencode.json, or JEV_DEBUG=1 in env.
 */

import { Plugin } from "@opencode/plugin";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";

// ---------------------------------------------------------------------------
// Schemas (synced from plugin/__init__.py — keep caps/defaults identical).
// ---------------------------------------------------------------------------

const DESCRIPTION =
  "Click, type, select, and scroll in one visible browser tab until a goal is done. " +
  "Use it for actions: open a menu, fill a form, press a button, like a post, follow a link. " +
  "Do not open another browser tool, take a screenshot, or read this plugin's source. " +
  "Write the goal as the whole task with its end state, for example " +
  "'Open Bookmarks from the left nav; done when the Bookmarks timeline shows'. " +
  "Several steps in one goal are fine. Do not split a task into one call per click. " +
  "To look at, list, or collect what is on the page, call jev_read instead; " +
  "it scrolls too (scrolls=N), so do not call jev_drive just to scroll and see more. " +
  "Pass url to navigate the driver's own tab. Omit url to stay on the current page. " +
  "The result has status (done or blocked), actions, why, reason, and page_text. " +
  "Reasons: max_steps, the budget ran out, check page_text and continue with a narrower goal. " +
  "click_not_sent or stale_page, the page moved before input; retry the same goal once. " +
  "toggle_undo, the next click would have undone an earlier one, so the first click worked. " +
  "model_blocked, the target is not visible here; scroll with jev_read or pass a url. " +
  "shell or weak_done, the page was still loading. " +
  "no_page, there is no driver tab; pass url. " +
  "Do not repeat a goal that was blocked twice; report what page_text shows. " +
  "background must stay false unless the user asks for a hidden browser. " +
  "The debug overlay is a plugin setting, not an argument.";

const PARAMETERS = {
  type: "object",
  additionalProperties: false,
  required: ["goal"],
  properties: {
    goal: { type: "string", description: "Natural-language goal for the current page." },
    url: {
      type: "string",
      description:
        "Navigate the driver's existing tab to this page. Omit to keep the current page. A new tab is opened only if the driver has no live tab.",
    },
    target: { type: "string", description: "Explicit CDP target id to attach (opt-in)." },
    max_steps: {
      type: "integer",
      description: "Tick budget (default 12, hard cap 30).",
      default: 12,
      minimum: 1,
      maximum: 30,
    },
    cdp_url: { type: "string", description: "CDP URL to attach. Ignored unless background is true." },
    background: {
      type: "boolean",
      description:
        "Attach to cdp_url instead of the visible pane. Off by default. Set true only when the user asks for a hidden browser. Does not launch a hidden browser.",
      default: false,
    },
    watch: {
      type: "boolean",
      description:
        "If true, stop when the user changes the page (their action wins). The browser pane is visible either way. Default false.",
      default: false,
    },
    timeout_s: {
      type: "integer",
      description: "Subprocess timeout in seconds (default 300, hard cap 900).",
      default: 300,
      minimum: 1,
      maximum: 900,
    },
  },
} as const;

const READ_DESCRIPTION =
  "Read the same visible tab jev_drive uses, without clicking. " +
  "Use it to see what is on the page, list posts, or collect JSON. " +
  "Omit script to get an outline of articles, headings, times, and links. " +
  "Then call again with script: an expression, a function, or a body with return, " +
  "for example `[...document.querySelectorAll('article')].map(a => a.innerText.slice(0, 280))`. " +
  "The value must be JSON-serializable. " +
  "scrolls moves down the page before the script runs (max 15) so one call can cover a long feed. " +
  "Pass url to navigate that tab first. Omit url to read the current page.";

const READ_PARAMETERS = {
  type: "object",
  additionalProperties: false,
  properties: {
    url: { type: "string", description: "Navigate the driver's tab here before reading. Omit to stay." },
    script: {
      type: "string",
      description:
        "JavaScript expression, function, or body with return. JSON-serializable result. Omit for an outline.",
    },
    scrolls: {
      type: "integer",
      description: "How many viewport scrolls to run before reading. Default 0, max 15.",
      default: 0,
      minimum: 0,
      maximum: 15,
    },
    background: {
      type: "boolean",
      description:
        "Attach to cdp_url instead of the visible pane. Off by default. Set true only when the user asks for a hidden browser. Does not launch a hidden browser.",
      default: false,
    },
    cdp_url: { type: "string", description: "CDP URL to attach. Ignored unless background is true." },
    timeout_s: {
      type: "integer",
      description: "Subprocess timeout in seconds (default 300, hard cap 900).",
      default: 300,
      minimum: 1,
      maximum: 900,
    },
  },
} as const;

// ---------------------------------------------------------------------------
// Core (port of plugin/handler.py — keep behavior identical).
// ---------------------------------------------------------------------------

const MAX_STEPS_CAP = 30;
const TIMEOUT_CAP = 900;
const DEFAULT_MAX_STEPS = 12;
const DEFAULT_TIMEOUT = 300;
const PAGE_TEXT_LIMIT = 2000;

type Args = Record<string, unknown>;

function truthy(v: unknown): boolean {
  return v === true || v === "true" || v === "True" || v === 1 || v === "1";
}

function budgetInt(value: unknown, lo: number, hi: number, name: string, fallback: number): number {
  // Strict: reject (don't silently clamp) so the caller knows the budget it got.
  if (value === undefined || value === null) return fallback;
  if (typeof value !== "number" || !Number.isInteger(value) || value < lo || value > hi) {
    throw new Error(`${name} must be an integer ${lo}..${hi}; no action executed.`);
  }
  return value;
}

function stoppedReason(status: string, reason: unknown, error: unknown): string {
  if (error === "timeout") return "time_budget";
  if (status === "done") return "model_done";
  if (reason === "max_steps") return "action_budget";
  if (status === "error") return "error";
  return "model_blocked";
}

function buildDriveArgv(args: Args): string[] {
  const maxSteps = budgetInt(args.max_steps, 1, MAX_STEPS_CAP, "max_steps", DEFAULT_MAX_STEPS);
  const argv = [
    "uv",
    "run",
    "python",
    "scripts/drive.py",
    "--json",
    "--goal",
    String(args.goal),
    "--max-steps",
    String(maxSteps),
  ];
  if (args.url) argv.push("--url", String(args.url));
  if (args.target) argv.push("--target", String(args.target));
  if (truthy(args.background)) {
    argv.push("--background");
    if (args.cdp_url) argv.push("--cdp", String(args.cdp_url));
  }
  if (truthy(args.watch)) argv.push("--watch");
  argv.push(truthy(args.debug) ? "--debug" : "--no-debug");
  return argv;
}

function buildReadArgv(args: Args): string[] {
  const scrolls = budgetInt(args.scrolls, 0, 15, "scrolls", 0);
  const argv = ["uv", "run", "python", "scripts/read.py", "--json"];
  if (args.url) argv.push("--url", String(args.url));
  if (args.script) argv.push("--script", String(args.script));
  argv.push("--scrolls", String(scrolls));
  if (args.target) argv.push("--target", String(args.target));
  if (truthy(args.background)) {
    argv.push("--background");
    if (args.cdp_url) argv.push("--cdp", String(args.cdp_url));
  }
  argv.push(truthy(args.debug) ? "--debug" : "--no-debug");
  return argv;
}

function parseJsonLines(text: string): Record<string, unknown>[] {
  const rows: Record<string, unknown>[] = [];
  for (const line of (text || "").split("\n")) {
    const t = line.trim();
    if (!t.startsWith("{")) continue;
    try {
      rows.push(JSON.parse(t));
    } catch {
      // skip non-JSON log lines
    }
  }
  return rows;
}

function sumUsage(rows: Record<string, unknown>[]) {
  const total = { input_tokens: 0, output_tokens: 0, cost: 0 };
  for (const row of rows) {
    const usage = row.usage as Record<string, unknown> | undefined;
    if (!usage || typeof usage !== "object") continue;
    total.input_tokens += Number(usage.input_tokens ?? 0) || 0;
    total.output_tokens += Number(usage.output_tokens ?? 0) || 0;
    total.cost += Number(usage.cost ?? 0) || 0;
  }
  return total;
}

function compactResult(rows: Record<string, unknown>[], exitCode: number, error?: string | null) {
  const meta = (rows.find((r) => r.event === "browser") ?? {}) as Record<string, unknown>;
  const ticks = rows.filter((r) => r.status);
  const last = (ticks[ticks.length - 1] ?? {}) as Record<string, unknown>;
  let status = (last.status as string) || (error ? "error" : "blocked");
  error = error || (last.error as string | undefined) || null;
  if (status !== "done" && status !== "blocked" && status !== "error") status = "blocked";
  const actions = ticks.map((t) => t.last_action).filter(Boolean);
  const browser: Record<string, unknown> = {
    source: meta.source,
    cdp_url: meta.cdp_url,
    auto_launched: Boolean(meta.auto_launched),
    visibility:
      meta.visibility || (meta.source === "terminal-browser" ? "terminal-browser-pane" : "unknown"),
  };
  if (meta.continuity) browser.continuity = meta.continuity;
  if (meta.log) browser.log = meta.log;
  const success = exitCode === 0 && status === "done" && !error;
  const out: Record<string, unknown> = {
    success,
    status: error && status !== "blocked" ? "error" : status,
    final_url: last.url,
    actions,
    ticks: ticks.length,
    usage: sumUsage(rows),
    browser,
    error,
  };
  if (last.page_text !== undefined && last.page_text !== null) {
    out.page_text =
      typeof last.page_text === "string" ? last.page_text.slice(0, PAGE_TEXT_LIMIT) : last.page_text;
  }
  if (last.why) out.why = last.why;
  if (last.reason) out.reason = last.reason;
  // DONE is a model choice, never an independent verification.
  out.verified = null;
  out.outcome_verification =
    out.status === "done" ? "unverified - DONE choice by model without independent check" : "not_applicable";
  out.stopped_reason = stoppedReason(out.status as string, out.reason, out.error);
  const insights = ticks.map((t) => t.insight).filter((i) => i && typeof i === "object");
  if (insights.length > 0) out.insights = insights;
  if (ticks.some((t) => t.degenerate)) out.degenerate = true;
  return out;
}

function driverHome(): string {
  const env = (process.env.JEV_DRIVER_HOME || "").trim();
  if (env) return resolve(env.replace(/^~(?=\/|$)/, homedir()));
  // Walk up from this file so a copied plugin still finds the checkout.
  let dir = dirname(new URL(import.meta.url).pathname);
  for (;;) {
    if (existsSync(join(dir, "scripts", "drive.py"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) return dirname(new URL(import.meta.url).pathname);
    dir = parent;
  }
}

function hasDecisionKey(): boolean {
  for (const v of ["DECISION_GATE_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY"]) {
    if ((process.env[v] || "").trim()) return true;
  }
  return false;
}

interface RunResult {
  stdout: string;
  stderr: string;
  code: number | null;
  timedOut: boolean;
}

function runSubprocess(argv: string[], cwd: string, timeoutS: number, signal?: AbortSignal): Promise<RunResult> {
  return new Promise((resolve) => {
    const child = spawn(argv[0], argv.slice(1), { cwd, env: process.env });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try {
        process.kill(-child.pid!, "SIGTERM");
      } catch {
        child.kill("SIGTERM");
      }
      setTimeout(() => {
        try {
          if (child.exitCode === null) process.kill(-child.pid!, "SIGKILL");
        } catch {
          // already gone
        }
      }, 2000);
      resolve({ stdout, stderr, code: null, timedOut: true });
    }, timeoutS * 1000);
    const onAbort = () => {
      clearTimeout(timer);
      if (settled) return;
      settled = true;
      child.kill("SIGTERM");
      resolve({ stdout, stderr, code: null, timedOut: true });
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (c: string) => (stdout += c));
    child.stderr?.on("data", (c: string) => (stderr += c));
    child.on("error", (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      resolve({ stdout, stderr: String(err), code: 127, timedOut: false });
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      resolve({ stdout, stderr, code: code ?? 0, timedOut: false });
    });
  });
}

async function executeDrive(rawArgs: Args, execCtx: { signal?: AbortSignal; debug: boolean }) {
  const args: Args = { ...rawArgs, debug: execCtx.debug }; // plugin setting wins, model flag ignored
  const home = driverHome();
  if (!args.goal || !String(args.goal).trim()) {
    return { content: JSON.stringify(compactResult([], 1, "goal is required")) };
  }
  if (!existsSync(join(home, "scripts", "drive.py"))) {
    return { content: JSON.stringify(compactResult([], 1, `drive.py missing under ${home}`)) };
  }
  let argv: string[];
  let timeoutS: number;
  try {
    timeoutS = budgetInt(args.timeout_s, 1, TIMEOUT_CAP, "timeout_s", DEFAULT_TIMEOUT);
    argv = buildDriveArgv(args);
  } catch (err) {
    return { content: JSON.stringify(compactResult([], 1, (err as Error).message)) };
  }
  const run = await runSubprocess(argv, home, timeoutS, execCtx.signal);
  if (run.timedOut) {
    const rows = parseJsonLines(run.stdout);
    const result = compactResult(rows, 1, "timeout");
    result.status = "blocked";
    result.success = false;
    result.stopped_reason = stoppedReason(result.status as string, result.reason, result.error);
    return { content: JSON.stringify(result) };
  }
  const rows = parseJsonLines(run.stdout);
  let error: string | null = null;
  if (run.code !== 0 && run.code !== 1 && rows.length === 0) {
    error = `driver exited ${run.code}`;
  } else if (run.code !== 0 && rows.length === 0) {
    const tail = run.stderr.trim().split("\n").slice(-15).join(" | ");
    error = "driver failed with no output" + (tail ? `: ${tail}` : "");
  }
  return { content: JSON.stringify(compactResult(rows, run.code ?? 0, error)) };
}

async function executeRead(rawArgs: Args, execCtx: { signal?: AbortSignal; debug: boolean }) {
  const args: Args = { ...rawArgs, debug: execCtx.debug };
  const home = driverHome();
  if (!existsSync(join(home, "scripts", "read.py"))) {
    return { content: JSON.stringify({ success: false, error: `read.py missing under ${home}` }) };
  }
  let argv: string[];
  let timeoutS: number;
  try {
    timeoutS = budgetInt(args.timeout_s, 1, TIMEOUT_CAP, "timeout_s", DEFAULT_TIMEOUT);
    argv = buildReadArgv(args);
  } catch (err) {
    return { content: JSON.stringify({ success: false, error: (err as Error).message }) };
  }
  const run = await runSubprocess(argv, home, timeoutS, execCtx.signal);
  if (run.timedOut) {
    return { content: JSON.stringify({ success: false, status: "blocked", error: "timeout" }) };
  }
  const rows = parseJsonLines(run.stdout);
  if (rows.length > 0) return { content: JSON.stringify(rows[rows.length - 1]) };
  const tail = run.stderr.trim().split("\n").slice(-8).join(" | ");
  return { content: JSON.stringify({ success: false, error: "read failed" + (tail ? `: ${tail}` : "") }) };
}

// ---------------------------------------------------------------------------
// Plugin entry.
// ---------------------------------------------------------------------------

export default Plugin.define({
  id: "jev-driver",
  async setup(ctx) {
    const debug =
      (ctx.options as Record<string, unknown> | undefined)?.debug === true ||
      ["1", "true", "yes"].includes((process.env.JEV_DEBUG || "").trim().toLowerCase());

    if (!hasDecisionKey()) {
      console.error("[jev-driver] no decision API key in env (TYPESAFE_API_KEY); tools will report errors until set.");
    }

    await ctx.tool.transform((editor) => {
      editor.add({
        name: "jev_drive",
        description: DESCRIPTION,
        input: PARAMETERS as unknown as Parameters<typeof editor.add>[0]["input"],
        execute: async (input, context) =>
          executeDrive(input as Args, { signal: context.signal, debug }),
      });
      editor.add({
        name: "jev_read",
        description: READ_DESCRIPTION,
        input: READ_PARAMETERS as unknown as Parameters<typeof editor.add>[0]["input"],
        execute: async (input, context) =>
          executeRead(input as Args, { signal: context.signal, debug }),
      });
    });
  },
});
