"""Live-test manifest: the schema, and the vocabulary the protocol is written in.

v3 of the design (`live-tests-design.md`) is the spec. This module owns only what
the manifest declares: a test id, where it runs, the goal text handed to the
driver, its safety tier, its timeout, and the end state it is scored against.
Everything downstream -- classification, byte accounting, the regression diff --
reads the shapes declared here rather than re-deriving them, so a manifest that
validates is a manifest every later stage can rely on.

Stdlib only, like the rest of the repo. Nothing here touches a browser or the
network: a manifest is data, and validating data must stay possible when the pane
is busy.
"""

from __future__ import annotations

# v3 "Harness": per-test timeout_s, default 300, per-test overrides.
DEFAULT_TIMEOUT_S = 300

# v3 "Harness": stall = no tick progress in 120s = MISS.
STALL_S = 120

# v3 "Safety tiers". X is never automated, so the runner refuses to carry it.
TIERS = ("R", "W", "X")
AUTOMATABLE_TIERS = ("R", "W")

# The four classifications, spelled as v3 spells them. `blocked_unjustified` is
# not a bucket a run lands in: v3 folds it into MISS, and the name exists so a
# reader can see the fold happened rather than wondering where it went.
HIT = "HIT"
MISS = "MISS"
BLOCKED_HONEST = "BLOCKED_HONEST"

# v3 also names a fourth outcome, BLOCKED-unjustified, and then says it equals
# MISS. So it is deliberately *not* a classification here: a blocked stop on a
# satisfiable goal is recorded as MISS and carries `unjustified_block: True`, so
# the scoreboard can still report the unjustified-block rate v3 asks for without
# inventing a bucket the protocol does not have.
CLASSIFICATIONS = (HIT, MISS, BLOCKED_HONEST)

# Stop reasons that count as a genuine finish. v3 accepts model_done and a P2
# rescue that reached the right end state; anything else is not a HIT.
DONE_STOP_REASONS = ("model_done", "end_state_reached")

# The fields every manifest test must declare. `expected` may be present but
# empty only for a test whose goal is genuinely unsatisfiable, which is checked
# in validate() rather than here, because it needs both fields together.
REQUIRED_FIELDS = ("id", "site", "goal", "tier", "satisfiable", "expected", "hit_line", "miss_line")

# v3 S6 redaction rule: presence-only for amounts on every S6x test. Recorded as
# a per-test flag rather than inferred from the id, so a new S6 test cannot
# forget the rule by being renamed.
REDACT_PRESENCE_ONLY = "presence_only"


class ManifestError(ValueError):
    """A manifest that cannot be run as written.

    Raised before any drive, so a typo in the manifest costs zero browser time
    rather than surfacing as a MISS that looks like a product failure.
    """


def _require(test: dict, field: str, where: str):
    if field not in test:
        raise ManifestError(f"{where}: missing required field {field!r}")
    return test[field]


def validate_test(test: dict, *, index: int | None = None) -> dict:
    """One manifest entry, checked and defaulted. Returns the normalized copy.

    Raises ManifestError on anything that would make a run unscorable: no id, an
    unknown tier, a timeout that is not a positive number, or a satisfiable goal
    with no end state to score against (which would silently make every outcome a
    MISS, or worse, every outcome a HIT).
    """
    where = f"test[{index}]" if index is not None else str(test.get("id", "<no id>"))
    if not isinstance(test, dict):
        raise ManifestError(f"{where}: a test must be an object")

    for field in REQUIRED_FIELDS:
        _require(test, field, where)

    tier = test["tier"]
    if tier not in TIERS:
        raise ManifestError(f"{where}: tier must be one of {TIERS}, got {tier!r}")
    if tier == "X":
        raise ManifestError(f"{where}: tier X is never automated (v3 safety tiers)")

    timeout = test.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ManifestError(f"{where}: timeout_s must be a positive int, got {timeout!r}")
    if timeout > STALL_S and "stall_s" not in test:
        # A timeout longer than the stall window is only meaningful if the runner
        # knows the stall window separately; otherwise the two silently disagree.
        test.setdefault("stall_s", STALL_S)

    expected = test["expected"]
    if not isinstance(expected, dict):
        raise ManifestError(f"{where}: expected must be an object")
    known = {"url_host_path", "final_view_contains", "url_contains"}
    unknown = set(expected) - known
    if unknown:
        raise ManifestError(f"{where}: unknown expected key(s) {sorted(unknown)}; known: {sorted(known)}")
    if test["satisfiable"] and not expected:
        raise ManifestError(f"{where}: a satisfiable goal needs an expected end state to score against")

    redact = test.get("redact")
    if redact is not None and redact != REDACT_PRESENCE_ONLY:
        raise ManifestError(f"{where}: redact must be {REDACT_PRESENCE_ONLY!r} if present")

    normalized = dict(test)
    normalized["timeout_s"] = timeout
    normalized.setdefault("stall_s", STALL_S)
    normalized.setdefault("redact", None)
    return normalized


def validate_manifest(manifest: dict) -> dict:
    """A whole manifest, checked end to end. Test ids must be unique.

    Duplicated ids would collide in the ledger (one row per run, keyed by run_id,
    derived from the id) and in the per-test baseline, so they are refused here
    rather than producing two rows that silently overwrite each other.
    """
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be an object")
    tests = manifest.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ManifestError("manifest needs a non-empty 'tests' list")

    seen: set[str] = set()
    normalized = []
    for index, test in enumerate(tests):
        checked = validate_test(test, index=index)
        if checked["id"] in seen:
            raise ManifestError(f"duplicate test id {checked['id']!r}")
        seen.add(checked["id"])
        normalized.append(checked)

    return {"suite": manifest.get("suite") or "live", "tests": normalized}