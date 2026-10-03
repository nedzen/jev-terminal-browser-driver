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

from scripts.live.taxonomy import (
    CAP_MANDATORY,
    MATRIX_CAPS,
    TaxonomyError,
    validate_anomaly_tags,
    validate_failure_cause,
)

# v3 "Harness": per-test timeout_s, default 300, per-test overrides.
DEFAULT_TIMEOUT_S = 300

# v3 "Harness": stall = no tick progress in 120s = MISS.
STALL_S = 120

# v3 "Safety tiers". X is never automated, so the runner refuses to carry it.
# Outcome classes live in taxonomy.py (v3.1 partition); this module only
# validates the manifest vocabulary.
TIERS = ("R", "W", "X")

# The fields every manifest test must declare. `expected` may be present but
# empty only for a test whose goal is genuinely unsatisfiable, which is checked
# in validate() rather than here, because it needs both fields together.
# `goal` is required *unless* `drives` is present, so it is checked separately below
# rather than listed here: a chain test declares its goals per drive and would
# otherwise be refused before the chain is ever read.
REQUIRED_FIELDS = ("id", "site", "tier", "satisfiable", "expected", "hit_line", "miss_line")

# v3 S6 redaction rule: presence-only for amounts on every S6x test. Recorded as
# a per-test flag rather than inferred from the id, so a new S6 test cannot
# forget the rule by being renamed.
REDACT_PRESENCE_ONLY = "presence_only"

# v3.1 machine predicates. `url_contains` and `text_present` are what make a
# verdict auto-judgeable; `final_view_contains` is the older spelling of
# `text_present` and is accepted so a v3 manifest still validates.
MACHINE_PREDICATES = ("url_host_path", "url_contains", "text_present", "final_view_contains")


class ManifestError(ValueError):
    """A manifest that cannot be run as written.

    Raised before any drive, so a typo in the manifest costs zero browser time
    rather than surfacing as a MISS that looks like a product failure.
    """


def _drive_step(step: dict, *, index: int, where: str, fallback_url) -> dict:
    """One link in a drive chain, validated.

    A step's `url` is genuinely optional: S4a/M9 is exactly the case of drive 2
    running *without* a url so the driver has to re-attach, and requiring one here
    would make the continuity test unrepresentable. The first step inherits the
    test-level url so a single-drive manifest reads the same either way.
    """
    label = f"{where}.drives[{index}]"
    if not isinstance(step, dict):
        raise ManifestError(f"{label}: each drive must be an object")
    goal = step.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ManifestError(f"{label}: a drive needs a goal")
    url = step.get("url")
    if url is not None and not isinstance(url, str):
        raise ManifestError(f"{label}: url must be a string when present")
    if index == 0 and url is None:
        url = fallback_url
    timeout = step.get("timeout_s")
    if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0):
        raise ManifestError(f"{label}: timeout_s must be a positive int")
    unknown = set(step) - {"goal", "url", "max_steps", "timeout_s", "stall_s", "note"}
    if unknown:
        raise ManifestError(f"{label}: unknown key(s) {sorted(unknown)}")
    out = {"goal": goal, "url": url, "note": step.get("note")}
    for field in ("max_steps", "timeout_s", "stall_s"):
        if step.get(field) is not None:
            out[field] = step[field]
    return out


def _chain(test: dict, where: str) -> list[dict]:
    """The drive chain for a test: an explicit `drives` list, or one implicit step.

    Always returns a list, even for a single-drive test, so the runner has exactly
    one code path. A test that declares both `goal` and `drives` is refused rather
    than silently preferring one: which of the two the author meant is not
    knowable here, and guessing it changes what the test measures.
    """
    drives = test.get("drives")
    if drives is None:
        return [_drive_step({"goal": test["goal"], "url": test.get("url")},
                            index=0, where=where, fallback_url=test.get("url"))]
    if test.get("goal"):
        raise ManifestError(f"{where}: declare either goal or drives, not both")
    if not isinstance(drives, list) or not drives:
        raise ManifestError(f"{where}: drives must be a non-empty list")
    if len(drives) == 1 and drives[0].get("url") is None:
        raise ManifestError(
            f"{where}: a single-drive chain needs a url, or use the test-level goal"
        )
    return [_drive_step(step, index=i, where=where, fallback_url=None)
            for i, step in enumerate(drives)]


def _chain_budget(test: dict, where: str, drives: list[dict]) -> float | None:
    """Wall-clock budget for the whole chain, across every drive in it.

    Separate from the per-call timeouts on purpose: three calls that each fit in
    their own timeout can still overrun the suite, and only a chain-level budget
    notices. Defaults to the sum of the per-call timeouts so it is never tighter
    than the calls it governs.
    """
    declared = test.get("chain_budget_s")
    if declared is not None:
        if not isinstance(declared, int) or isinstance(declared, bool) or declared <= 0:
            raise ManifestError(f"{where}: chain_budget_s must be a positive int")
        return declared
    total = 0
    for step in drives:
        total += int(step.get("timeout_s") or test.get("timeout_s") or DEFAULT_TIMEOUT_S)
    return total


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
    if test.get("_validated") and isinstance(test.get("drives"), list):
        # Idempotent re-entry: run_suite validates the manifest, then run_test
        # validates each test again. Without this guard the second pass sees
        # the normalized copy (goal + derived drives) and refuses it as
        # ambiguous — which made every goal-style manifest unrunnable.
        return dict(test)
    if not isinstance(test, dict):
        raise ManifestError(f"{where}: a test must be an object")

    for field in REQUIRED_FIELDS:
        _require(test, field, where)

    # Presence, not truthiness: `drives: []` must reach `_chain` and be reported as
    # an empty chain, not as a missing goal.
    if "drives" not in test and not test.get("goal"):
        raise ManifestError(f"{where}: missing required field 'goal' (or declare drives: [...])")
    if test.get("goal") is not None and not isinstance(test["goal"], str):
        raise ManifestError(f"{where}: goal must be a string")

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
    unknown = set(expected) - set(MACHINE_PREDICATES)
    if unknown:
        raise ManifestError(
            f"{where}: unknown expected key(s) {sorted(unknown)}; known: {sorted(MACHINE_PREDICATES)}"
        )
    # v3.1: auto-judged where a machine predicate exists, human-judged otherwise.
    # Checked before the "needs an end state" rule, because with human_judged set
    # an empty `expected` is legitimate -- the run simply waits for a person.
    human_judged = bool(test.get("human_judged"))
    if test["satisfiable"] and not expected and not human_judged:
        raise ManifestError(
            f"{where}: a satisfiable goal needs either a machine predicate or human_judged: true"
        )
    if human_judged and expected:
        raise ManifestError(
            f"{where}: human_judged and a machine predicate are alternatives, not both"
        )

    deny_actions = test.get("deny_actions") or []
    deny_elements = test.get("deny_elements") or []
    for field, values in (("deny_actions", deny_actions), ("deny_elements", deny_elements)):
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ManifestError(f"{where}: {field} must be a list of strings")

    matrix = test.get("matrix") or {}
    if not isinstance(matrix, dict):
        raise ManifestError(f"{where}: matrix must be an object")
    cap = matrix.get("cap", CAP_MANDATORY)
    if cap not in MATRIX_CAPS:
        raise ManifestError(f"{where}: matrix.cap must be one of {MATRIX_CAPS}, got {cap!r}")

    comparative = test.get("comparative") or {}
    if not isinstance(comparative, dict):
        raise ManifestError(f"{where}: comparative must be an object")
    for field in ("ab_cell", "wording_cell"):
        if field in comparative and not isinstance(comparative[field], str):
            raise ManifestError(f"{where}: comparative.{field} must be a string")

    try:
        tags = validate_anomaly_tags(test.get("anomaly_tags"))
    except TaxonomyError as exc:
        raise ManifestError(f"{where}: {exc}") from exc
    cause = test.get("failure_cause")
    if cause is not None:
        # The explanation is handed to the taxonomy rather than re-checked here:
        # `other` without one is already refused by validate_failure_cause, so a
        # second check would be a branch nothing could reach.
        try:
            validate_failure_cause(cause, test.get("failure_cause_explain"))
        except TaxonomyError as exc:
            raise ManifestError(f"{where}: {exc}") from exc

    fingerprint = test.get("site_fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, str):
        raise ManifestError(f"{where}: site_fingerprint must be a string")

    redact = test.get("redact")
    if redact is not None and redact != REDACT_PRESENCE_ONLY:
        raise ManifestError(f"{where}: redact must be {REDACT_PRESENCE_ONLY!r} if present")

    normalized = dict(test)
    normalized["timeout_s"] = timeout
    normalized.setdefault("stall_s", STALL_S)
    normalized.setdefault("redact", None)
    normalized["human_judged"] = human_judged
    normalized["drives"] = _chain(test, where)
    if not normalized.get("goal"):
        # Give the normalized copy a goal so every downstream consumer of `goal`
        # (the ledger row, the run record, the slice) keeps working for a chain.
        # The manifest-facing rule stays one-or-the-other; this is derived text.
        normalized["goal"] = " -> ".join(step["goal"] for step in normalized["drives"])
    normalized["chain_budget_s"] = _chain_budget(test, where, normalized["drives"])
    normalized["deny_actions"] = [a.lower() for a in deny_actions]
    normalized["deny_elements"] = [e.lower() for e in deny_elements]
    normalized["matrix"] = {"cap": cap, **matrix}
    normalized["comparative"] = dict(comparative)
    normalized["anomaly_tags"] = list(tags)
    normalized["site_fingerprint"] = fingerprint
    normalized["_validated"] = True
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