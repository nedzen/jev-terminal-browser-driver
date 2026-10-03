"""The run-log record builder: what survives after the process exits.

A trace record is not a tick row and not an agent result, and it is built here
rather than in result.py because the three disagree in a way that is invisible
until a run is compared byte for byte:

- a tick row and an agent result *omit* a field that has nothing to say, so the
  wire stays small. A trace record writes the key with an explicit null, because
  a log reader asks "was this run's reason recorded?" and "absent" and "null"
  are not the same answer.
- the record carries the decision's ranked operations and targets, which exist
  nowhere else, and it caps page text at its own limit rather than the row's.
- the ordering is the log's on-disk order, so it is data, not dict-literal luck.

`write_event` stays in jev_driver/runlog.py on purpose. That module is the
sanitize-and-append boundary and the redaction vocabulary, and it also serves
`redact_for_wire`, which is a property of an outgoing request body rather than
of this record. Moving it would put a wire concern in the leaf both adapters
load and would break every module that patches its module globals.

Stdlib only. See plugin/core/result.py for why that is a hard rule.
"""

from __future__ import annotations

from dataclasses import dataclass

TRACE_PAGE_TEXT = 1500

# Ranked operations/targets are the point of the trace, so they are read deeper
# than the tick row's insight copy, which the agent pays for on every step.
TRACE_PROBS_LIMIT = 8


@dataclass(frozen=True)
class TraceField:
    """One declared trace field.

    ``present`` is the difference from result.Field: False means the key is
    written only when it carries something, True means the key is always there
    and a missing value is an explicit null. ``cap`` is this record's own size
    limit, and ``truthy`` collapses an empty value to null instead of writing
    "" or 0 into an append-only log that a human reads.
    """

    name: str
    cap: int | None = None
    truthy: bool = False
    present: bool = True

    def render(self, value):
        if self.cap is not None and isinstance(value, str):
            value = value[: self.cap]
        if self.truthy and not value:
            return None
        return value

    def carries(self, value) -> bool:
        return self.present or bool(value)


# The log's field order, which is its on-disk order. event/goal lead because a
# reader scanning the file looks for them first.
TRACE_FIELDS = (
    TraceField("event"),
    TraceField("goal"),
    TraceField("status"),
    TraceField("url"),
    TraceField("last_action"),
    TraceField("why"),
    TraceField("error"),
    TraceField("degenerate", truthy=True),
    # Decision provenance, so a run is attributable and a drift is visible in the
    # log rather than inferred from it. All four are `present`: a null is the
    # answer "the provider did not report this", which is the finding, and it has
    # to be distinguishable from an older record written before the field existed.
    TraceField("model"),
    TraceField("model_version"),
    TraceField("confidence"),
    TraceField("question_spec_hash"),
    TraceField("ranked_ops"),
    TraceField("ranked_targets"),
    TraceField("page_text", cap=TRACE_PAGE_TEXT, truthy=True),
    TraceField("reason"),
)
TRACE_BY_NAME = {field.name: field for field in TRACE_FIELDS}

# The one conditional field: a run that never re-read the page has no final
# view, and writing an empty one would claim it looked.
TRACE_CONDITIONAL = (TraceField("final_view", truthy=True, present=False),)


def trace_kind(status, error) -> str:
    """The event name this tick gets in the log.

    A tick that errored reads as blocked whatever its status claimed: the log
    exists to explain a run that did not finish, and a "tick" line next to an
    error is the one line nobody can act on.
    """
    if error or status == "blocked":
        return "blocked"
    if status == "done":
        return "done"
    return "tick"


def label_of(text, fallback, width=60) -> str:
    """A criterion's readable label: drop the "[KEY]" prefix and the ";" tail, clipped to width."""
    raw = str(text or fallback or "")
    if raw.startswith("[") and "]" in raw:
        raw = raw.split("]", 1)[1]
    return raw.split(";")[0].strip()[:width]


def target_labels(decision) -> dict:
    """The decision's target ids as readable labels, keyed by id."""
    questions = ((decision or {}).get("request") or {}).get("questions") or {}
    op_key = ((decision or {}).get("operation") or "").lower() + "_target"
    criteria = (questions.get(op_key) or {}).get("criteria") or {}
    return {key: label_of(text, key, 80) for key, text in criteria.items()}


def top_probs(probs, labels=None, limit=4) -> list[dict]:
    """The ranked head of a probability dict, labeled and rounded."""
    labels = labels or {}
    items = sorted((probs or {}).items(), key=lambda kv: -float(kv[1] or 0))[:limit]
    return [{"name": labels.get(key, key), "p": round(float(val), 3)} for key, val in items]


def last_decision(snap: dict) -> dict:
    """The decision a snapshot ended on, or an empty one."""
    decisions = snap.get("decisions") or []
    return decisions[-1] if decisions else {}


def decision_provenance(decision: dict) -> dict:
    """Which model answered, which version of it, how sure it was, and under which prompt.

    ``model`` is the id this run *asked* for, read off the recorded request rather
    than off the response, because the resolved id is the operator's configuration
    and the response's own ``model`` is the provider's echo of it. Keeping the two
    apart is what makes a silent substitution visible instead of plausible.
    """
    decision = decision or {}
    request = decision.get("request") or {}
    return {
        "model": request.get("model"),
        "model_version": decision.get("model_version"),
        "confidence": decision.get("confidence"),
        "question_spec_hash": decision.get("question_spec_hash"),
    }


def build_trace_record(rec: dict, decision: dict, *, goal: str) -> dict:
    """One run-log record: the tick row's facts, plus what the decision ranked.

    ``rec`` is the tick row as `build_tick_row` produced it and ``decision`` the
    snapshot's last decision, so the record reads the row rather than reaching
    back into the snapshot for the same facts twice.
    """
    rec = rec or {}
    decision = decision or {}
    labels = target_labels(decision)
    # The row is the record's base, so a field declared below is filled from
    # the row by default; only the four the log computes itself are replaced.
    # A field added to a table therefore needs no edit here, which is the point
    # of the table: the alternative is a name in this dict that the table and
    # the log reader can disagree about.
    values = {
        **rec,
        "event": trace_kind(rec.get("status"), rec.get("error")),
        "goal": goal,
        **decision_provenance(decision),
        "ranked_ops": top_probs(decision.get("operation_probabilities"), limit=TRACE_PROBS_LIMIT),
        "ranked_targets": top_probs(decision.get("target_probabilities"), labels, limit=TRACE_PROBS_LIMIT),
    }
    record = {field.name: field.render(values.get(field.name)) for field in TRACE_FIELDS}
    for field in TRACE_CONDITIONAL:
        value = field.render(values.get(field.name))
        if field.carries(value):
            record[field.name] = value
    return record