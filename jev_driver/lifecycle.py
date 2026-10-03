"""Instance provenance, root-terminal placement, and the idle-instance reaper.

Three separate questions live here because they share one fact -- *who opened this
browser* -- and today nothing on disk records it.

**Provenance.** `Discovery.auto_launched` is an in-memory dataclass field. Once the
process exits, a browser the driver opened is indistinguishable from one the human
opened, which is exactly the distinction a reaper must not get wrong. So the
driver-spawned flag is written into the continuity record and read back.

**Root-terminal placement.** terminal-browser splits the *current* surface. When the
driver runs inside a herdr pane that is itself a cmux surface, splitting the current
surface nests the browser inside the agent's own pane. Adapter detection is not the
problem and cannot be fixed there: the cmux adapter reports the surface hosting the
herdr pane, and no terminal-browser adapter can open a root-level tab (see
`root_terminal_blocker`). So provisioning refuses, and names the remedy.

**Reaping.** Driver-spawned and idle for longer than `INSTANCE_IDLE_TTL_S` with no
lease is reaped. Everything else is kept. An instance whose origin cannot be
established is *kept* -- an idle browser is a cost, a closed user tab is data loss.

The two TTLs are deliberately not one. `browser.LAST_PAGE_TTL_S` (1800s) ages a
continuity record so a stale tab is not re-attached to; `INSTANCE_IDLE_TTL_S` (900s)
ages a live browser process so an abandoned one is reclaimed. Different objects,
different clocks, different consequences.
"""

from __future__ import annotations

import os
import time

# A driver-spawned browser nobody has driven for this long is abandoned. 15 minutes
# is comfortably longer than any single run's outer budget, so a live run is never at
# risk from this; it exists to reclaim browsers left by a crashed or abandoned drive.
INSTANCE_IDLE_TTL_S = 900

# Origins. `unknown` is a real answer, not a placeholder: it is what a browser opened
# outside the driver looks like, and it is the state the reaper refuses to act on.
ORIGIN_DRIVER = "driver-spawned"
ORIGIN_OWNER = "owner-opened"
ORIGIN_UNKNOWN = "unknown"

# Verdicts.
REAP = "reap"
KEEP = "keep"

# Variables that carry a herdr trace without starting with HERDR_. Verified against
# the live environment: `SSH_AUTH_SOCK` points at ~/.config/herdr/herdr.sock.agent and
# `TERM_PROGRAM` is literally "herdr". Scrubbing only the HERDR_ prefix leaves both.
NON_PREFIXED_HERDR_VARS = ("SSH_AUTH_SOCK", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")

_HERDR_SOCKET_MARKERS = ("herdr",)


def is_nested_in_herdr(env=None) -> bool:
    """True when this process is running inside a herdr pane."""
    env = os.environ if env is None else env
    return bool(env.get("HERDR_PANE_ID"))


def scrubbed_env(env=None) -> dict:
    """Child env with every herdr trace removed, not just the HERDR_* prefix.

    The prefix scrub is necessary and not sufficient. Two live variables identify
    herdr without the prefix -- `SSH_AUTH_SOCK` (herdr's agent socket) and
    `TERM_PROGRAM=herdr` -- and either is enough for a child to conclude it is in a
    herdr pane. `SSH_AUTH_SOCK` is removed rather than blanked: a socket path that
    points nowhere is worse than an absent one, because an agent that finds it
    unconnectable falls back to a different auth path rather than reporting no agent.

    `PWD`/`OLDPWD` are left alone even when they mention `.herdr`: they are the
    caller's working directory, which the child legitimately needs, and a worktree
    path is not a terminal signal.
    """
    env = os.environ if env is None else env
    kept = {}
    for key, value in env.items():
        if key.startswith("HERDR_"):
            continue
        if key in NON_PREFIXED_HERDR_VARS and _mentions_herdr(value):
            continue
        kept[key] = value
    return kept


def _mentions_herdr(value) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in _HERDR_SOCKET_MARKERS)


def root_terminal_blocker(env=None) -> str | None:
    """Why provisioning cannot proceed at root level from here, or None if it can.

    Returns a human-readable reason rather than a bool so the caller can put the
    actual remedy in the operator's hands, which is the whole point of refusing
    instead of nesting.

    terminal-browser cannot be told to open a root-level tab. Its adapter chain
    (`@zenbu-labs/pixel`) exposes split/sendText/focusPane and nothing that creates a
    tab, and `open` takes only `--split <direction>`, which always acts on the current
    surface. So the browser half of the fix is not available at this version; the
    remedy belongs to whoever owns the outer terminal (cmux calls tabs "workspaces":
    `cmux new-workspace --command ...`).
    """
    env = os.environ if env is None else env
    if not is_nested_in_herdr(env):
        return None
    outer = env.get("CMUX_SURFACE_ID") and "cmux" or env.get("TERM_PROGRAM") or "the outer terminal"
    return (
        f"refusing to provision inside a herdr pane: terminal-browser splits the current "
        f"surface, and this surface belongs to herdr, so the browser would nest inside the "
        f"agent's own pane. This is only reached when cmux cannot be addressed directly (no "
        f"CMUX_SOCKET_PATH + CMUX_WORKSPACE_ID); in a cmux workspace the cmux route runs "
        f"instead and creates the split at cmux level. Otherwise, open a root-level tab in "
        f"{outer} and run the drive there (cmux new-workspace --command ...). Scrubbing "
        f"HERDR_* is not the fix: it changes which adapter terminal-browser picks but not "
        f"which surface it splits."
    )


def instance_origin(record, *, driver_spawned=None) -> str:
    """Who opened this browser instance.

    `driver_spawned` is the caller's own knowledge for the instance it just opened --
    authoritative, because the flag is in hand. Otherwise the continuity record is
    consulted, and an absent or non-boolean flag is `unknown` rather than False: a
    record written before the flag existed must not read as "the human opened this".
    """
    if driver_spawned is True:
        return ORIGIN_DRIVER
    if driver_spawned is False:
        return ORIGIN_OWNER
    if not isinstance(record, dict):
        return ORIGIN_UNKNOWN
    flag = record.get("auto_launched")
    if flag is True:
        return ORIGIN_DRIVER
    if flag is False:
        return ORIGIN_OWNER
    return ORIGIN_UNKNOWN


def is_leased(instance, leased_keys=(), leased_ports=()) -> bool:
    """Whether some live run holds this instance.

    Both identifiers are accepted because they are what the two sides actually hold:
    the lease dict carries a terminal-browser `key`, and the CDP layer carries a port.
    A blank string is never a match -- `None in (...)` style membership against an
    unset field would otherwise report every instance as leased.
    """
    keys = {str(k) for k in leased_keys or () if k}
    ports = {int(p) for p in leased_ports or () if p}
    if not keys and not ports:
        return False
    instance_key = instance.get("key") if isinstance(instance, dict) else None
    if instance_key and str(instance_key) in keys:
        return True
    port = instance.get("cdp_port") if isinstance(instance, dict) else None
    try:
        if port is not None and int(port) in ports:
            return True
    except (TypeError, ValueError):
        return False
    return False


def _age_s(now, started) -> float | None:
    """Seconds since `started`, or None when it cannot be read.

    The daemon DB stores `started_at` in milliseconds (verified against a live row:
    1791020916093), while `now` here is seconds. Comparing them raw yields an age of
    about -1.8e9, which reads as "not idle yet" for every instance forever -- a reaper
    that silently never reaps anything. The threshold below 1e11 cleanly separates
    seconds (1.8e9 now) from milliseconds (1.8e12 now) with nine orders of magnitude
    of headroom on both sides.
    """
    try:
        value = float(started)
    except (TypeError, ValueError):
        return None
    if value > 1e11:
        value /= 1000.0
    return now - value


def reap_plan(
    instances,
    *,
    now=None,
    idle_ttl_s: int = INSTANCE_IDLE_TTL_S,
    last_page=None,
    leased_keys=(),
    leased_ports=(),
) -> list[dict]:
    """Decide, per instance, whether an idle sweep would close it. Decides; never acts.

    Every branch returns a reason, because "why did the sweep leave that one alone" is
    the question an operator actually has. The ordering is the safety ordering: origin
    is settled before idleness is even considered, so an instance of unknown origin is
    never reaped for being old.

    `last_page` is the continuity record, consulted for provenance only when the
    caller has no first-hand knowledge. It is matched on the browser id, not the tab
    id: the question is who opened the *browser*.
    """
    now = time.time() if now is None else now
    plan = []
    for instance in instances or ():
        instance = instance if isinstance(instance, dict) else {}
        identity = instance.get("browser_id")
        record = None
        if isinstance(last_page, dict) and identity and last_page.get("browser_id") == identity:
            record = last_page
        origin = instance_origin(record, driver_spawned=instance.get("_driver_spawned"))
        port = instance.get("cdp_port")
        entry = {
            "key": instance.get("key"),
            "cdp_port": port,
            "browser_id": identity,
            "origin": origin,
            "pid": instance.get("pid"),
            "started_at": instance.get("started_at"),
        }
        if origin == ORIGIN_UNKNOWN:
            entry.update(verdict=KEEP, reason="origin not established; doubt leaves it alone")
            plan.append(entry)
            continue
        if origin == ORIGIN_OWNER:
            entry.update(verdict=KEEP, reason="owner-opened; never reaped")
            plan.append(entry)
            continue
        if is_leased(instance, leased_keys, leased_ports):
            entry.update(verdict=KEEP, reason="held by an active lease")
            plan.append(entry)
            continue
        started = instance.get("started_at")
        age = _age_s(now, started)
        if age is None:
            entry.update(verdict=KEEP, reason="no usable start time; cannot prove idleness")
            plan.append(entry)
            continue
        entry["idle_s"] = round(age, 1)
        if age > idle_ttl_s:
            entry.update(verdict=REAP, reason=f"driver-spawned and idle {age:.0f}s > {idle_ttl_s}s")
        else:
            entry.update(verdict=KEEP, reason=f"driver-spawned but idle only {age:.0f}s")
        plan.append(entry)
    return plan


def strays(plan) -> list[dict]:
    """The instances a sweep would actually close."""
    return [entry for entry in plan or () if entry.get("verdict") == REAP]