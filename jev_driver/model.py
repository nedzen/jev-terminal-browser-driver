"""Jev on TypeSafe makes choices; a separate chat model writes field values."""

import hashlib
import json
import math
import os
import time
from pathlib import Path

import httpx

from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)
DEFAULT_BASE = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-1.13.0"
LABEL_MAX = 80
VALUE_MAX = 40
# Sum-of-probabilities slack. Loose enough for a rounded backend answer, tight
# enough that a head still has to commit to a distribution.
PROBABILITY_TOLERANCE = 0.05
BACKEND_MODEL = "typesafe"
BACKEND_DETERMINISTIC = "deterministic"
CALIBRATION_SURFACE = "typesafe_cloud"
# A head that was skipped because it had one candidate, and whose decision some
# other operation won instead. Distinct from "bypass": no answer was needed, so
# there is no number a consumer could mistake for a model's.
NOT_EXECUTED = "not-executed"
_DECISION_KEYS = ("DECISION_GATE_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY")
_TEXT_KEYS = ("TEXT_MODEL_API_KEY", "OPENROUTER_API_KEY")
# Fixed name/prompt pairs: the digest must not depend on dict order, and a
# prompt swapped between names must not collide with its neighbour.
_SPEC_NAMES = ("NEXT_ACTION", "TARGET", "TEXT_VALUE")
_SPEC_PROMPTS = (NEXT_ACTION, TARGET, TEXT_VALUE)


def question_spec_hash(prompts=None):
    """sha256 over the shipped decision prompts, truncated to 16 hex chars.

    Identifies which instruction text produced a decision, so a calibration
    sample can be attributed to a prompt revision. Callers pass ``prompts`` to
    prove sensitivity; the default is the spec questions.py ships now.
    """
    if prompts is None:
        pairs = tuple(zip(_SPEC_NAMES, _SPEC_PROMPTS))
    else:
        prompts = tuple(prompts)
        if len(prompts) != len(_SPEC_NAMES):
            raise ValueError(f"question_spec_hash takes {len(_SPEC_NAMES)} prompts")
        pairs = tuple(zip(_SPEC_NAMES, prompts))
    blob = "".join(f"{name}\x1f{prompt}\x1e" for name, prompt in pairs)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


QUESTION_SPEC_HASH = question_spec_hash()


def decisions_url() -> str:
    explicit = os.environ.get("DECISION_GATE_URL", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("TYPESAFE_BASE_URL", DEFAULT_BASE).strip().rstrip("/")
    if base.endswith("/v1/systemone"):
        return base
    return base + "/v1/systemone"


def explicit_endpoint(name: str) -> str | None:
    """An explicitly configured endpoint for ``name``, or None.

    Setting the variable is the operator naming a server and accepting its
    contract, which is the only thing that makes operating without a
    credential sensible: with nothing set the URL is a derived hosted default
    (api.typesafe.ai, openrouter.ai), and a keyless call there is an
    unauthenticated request to a third party rather than a local convenience.
    Point the variable at a hosted service and you get the same rejection any
    missing credential earns — loudly, and before a decision is acted on.
    """
    try:
        return os.environ.get(name, "").strip() or None
    except Exception:
        return None


def _env_files():
    homes = [Path.home() / ".hermes" / ".env"]
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        homes.append(Path(hermes_home).expanduser() / ".env")
    return homes


def _key_from_env_files(names: tuple[str, ...]) -> str | None:
    for name in names:
        prefix = name + "="
        for path in _env_files():
            if not path.is_file():
                continue
            for line in path.read_text().splitlines():
                if not line.startswith(prefix):
                    continue
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return None


def _key_from_environ(names: tuple[str, ...]) -> str | None:
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return None


def load_decision_key() -> str | None:
    url = decisions_url()
    names = _DECISION_KEYS
    if "openrouter.ai" in url:
        names = ("DECISION_GATE_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY")
    return _key_from_environ(names) or _key_from_env_files(names)


def load_text_key() -> str | None:
    return _key_from_environ(_TEXT_KEYS) or _key_from_env_files(_TEXT_KEYS)


def post_json(url, key, body):
    # An unauthenticated local backend must not receive a "Bearer " header:
    # an empty credential is no credential, not a malformed one. Deciding that
    # the credential may be empty at all belongs to the callers (`choose`,
    # `field_text`), which allow it only for an explicitly configured endpoint;
    # this function only renders the key it was handed.
    headers = {"Authorization": f"Bearer {key}"} if key else None
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers=headers)
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 503, 504, 529} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        try:
            data = response.json()
        except ValueError:
            # 2xx with a non-JSON or empty body (HTML gateway/CDN error page):
            # a transient provider failure, not a usable response.
            if attempt < 2:
                time.sleep(0.5 * 2**attempt)
                continue
            raise RuntimeError("Model provider returned an invalid response body; no action executed.")
        error = data.get("error") if isinstance(data, dict) else None
        if error:
            message = error.get("message", error) if isinstance(error, dict) else error
            # Some gateways emit string-typed codes ("503"); normalize.
            if attempt < 2 and isinstance(error, dict) and str(error.get("code")) in {"429", "503", "504", "529"}:
                time.sleep(0.5 * 2**attempt)
                continue
            raise RuntimeError(f"Model provider returned an error: {message}")
        return data
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        # Some backends name the picked option `decision`, others `choice`.
        chosen = answer["choice"] if "choice" in answer else answer["decision"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            chosen in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < PROBABILITY_TOLERANCE
            and probabilities[chosen] >= max(probabilities.values()) - 1e-6
            # Both keys present and disagreeing is a contradictory answer, not a
            # preference to resolve.
            and ("choice" not in answer or "decision" not in answer or answer["choice"] == answer["decision"])
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    # A copy: raw_answers must keep exactly what the backend sent, while callers
    # always read the winner under "choice".
    return {**answer, "choice": chosen}


def sole_candidate_answer(candidates):
    """The only legal answer for a head with one candidate, and its tag.

    Held to validate_choice's contract by the caller, so tightening validation
    breaks the bypass loudly instead of letting it emit a shape nothing else
    would have produced.
    """
    (index,) = candidates
    return {"choice": index, "confidence": 1.0, "probabilities": {index: 1.0}}


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def short_criterion(index, action):
    label = (action.get("label") or "")[:LABEL_MAX]
    bits = [f"[{index}] {label}"]
    if action.get("role"):
        bits.append(str(action["role"]))
    current = action.get("current_value", action.get("value", ""))
    if current:
        bits.append(f"value={str(current)[:VALUE_MAX]}")
    for key in ("checked", "selected", "expanded"):
        if key in action:
            bits.append(f"{key}={action[key]}")
    return "; ".join(bits)


def choose(state, goal, history):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    # A head with a single candidate has no choice to make: asking only buys a
    # hedged probability the agent cannot act on. Those heads are not asked at
    # all, and their answer is synthesized and tagged deterministic so a
    # synthetic 1.0 is never mistaken for a model confidence. The operation head
    # is always asked (DONE and BLOCKED are always candidates), so a decision is
    # never fully deterministic.
    bypassed = {operation for operation, candidates in targets.items() if len(candidates) == 1}
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        if operation in bypassed:
            continue
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {index: short_criterion(index, action) for index, action in candidates.items()},
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": os.environ.get("DECISION_GATE_MODEL") or os.environ.get("TYPESAFE_DEFAULT_MODEL") or DEFAULT_MODEL,
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    key = load_decision_key()
    if not key and not explicit_endpoint("DECISION_GATE_URL"):
        raise RuntimeError("Set TYPESAFE_API_KEY; no action executed.")
    started = time.perf_counter()
    result = post_json(decisions_url(), key, body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    # Per-question provenance. A consumer calibrating on confidence reads
    # stages[<head>]["backend"]: "deterministic" numbers are ours, not the
    # model's, however plausible they look.
    stages = {"operation": {"backend": BACKEND_MODEL, "source": "model"}}
    if operation in targets:
        if operation in bypassed:
            target_answer = validate_choice(sole_candidate_answer(targets[operation]), targets[operation])
            target = target_answer["choice"]
            stages[operation] = {
                "backend": BACKEND_DETERMINISTIC,
                "source": "bypass",
                "target": target,
                "confidence": target_answer["confidence"],
                "probabilities": dict(target_answer["probabilities"]),
            }
        else:
            answer = result["answers"].get(operation.lower() + "_target", {})
            target_answer = validate_choice(answer, targets[operation])
            target = target_answer["choice"]
            stages[operation] = {"backend": BACKEND_MODEL, "source": "model"}
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    # Every bypassed head gets an entry, not only the one this decision executed.
    # `bypassed_heads` and `decision_source: mixed` claim heads were answered in
    # process, and a record that names a bypass with no stage for it cannot be
    # checked without replaying the request. An unexecuted bypass carries no
    # numbers at all — that is the point of it — so it says so and stops there.
    # Sorted, because insertion order is what the run log serialises.
    for head in sorted(bypassed):
        stages.setdefault(head, {"backend": BACKEND_DETERMINISTIC, "source": NOT_EXECUTED})
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
        # backend names the surface that produced the executed choice (the
        # operation head is always the model's; a bypassed target head is not).
        "backend": BACKEND_DETERMINISTIC if operation in bypassed else BACKEND_MODEL,
        "model_version": result.get("model_version") or result.get("version"),
        "question_spec_hash": QUESTION_SPEC_HASH,
        "decision_source": "mixed" if bypassed else "model",
        "calibration_surface": CALIBRATION_SURFACE,
        # Every head that was answered in process, not just the executed one, so
        # "mixed" is auditable without replaying the request. `stages` carries the
        # matching per-head record: a bypass the model chose is `bypass`, a bypass
        # another operation won instead is `not-executed`.
        "bypassed_heads": sorted(bypassed),
        "stages": stages,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


class _PermanentError(ValueError):
    """Deterministic field_text failure (bad shape, empty/too-long value).

    Retrying cannot help: the helper answered, and the answer is unusable.
    A ValueError subclass so existing callers handle it identically.
    """


def _request_retryable(exc: RuntimeError) -> bool:
    """Retry transient provider failures, never deterministic ones.

    429/5xx may clear; other 4xx (auth, bad request) and an exhausted
    post_json ("Model unavailable" already spans its own retries) will not.
    """
    msg = str(exc)
    if "HTTP 429" in msg:
        return True
    if "HTTP 4" in msg:
        return False
    return "Model unavailable" not in msg


def _strip_code_fences(text):
    """Remove a surrounding markdown code fence (with or without a language tag)."""
    if not isinstance(text, str):
        raise ValueError("Text helper response was not a string")
    lines = text.strip().splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def field_text(context):
    """The value to type, from the text helper, or a refusal that types nothing.

    A keyless call is allowed only for an explicitly configured endpoint (see
    ``explicit_endpoint``): naming the server is how an operator says it takes no
    bearer token. Without one, the endpoint is the hosted default and the missing
    credential is the operator's to fix, so the helper refuses instead of sending
    an unauthenticated request.
    """
    key = load_text_key()
    endpoint = explicit_endpoint("TEXT_MODEL_BASE_URL")
    base = (endpoint or "https://openrouter.ai/api/v1").rstrip("/")
    if not key and not endpoint:
        raise ValueError(
            "Typing needs OPENROUTER_API_KEY (Plugins > wwwdrive > OpenRouter API key) or TEXT_MODEL_API_KEY. "
            "No text was typed."
        )
    model = os.environ.get("TEXT_MODEL", "inception/mercury-2.5")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING", "none") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    for attempt in range(3):
        content = None
        try:
            result = post_json(
                base + "/chat/completions",
                key,
                {
                    "model": model,
                    "max_tokens": 1024,
                    "response_format": {"type": "json_object"},
                    **reasoning,
                    "messages": [
                        {"role": "system", "content": TEXT_VALUE},
                        {
                            "role": "user",
                            "content": json.dumps(context),
                        },
                    ],
                },
            )
            # Request failures (retryable RuntimeError from post_json) and
            # structurally malformed 200s (missing choices/message/content)
            # are retried: both are transient provider failures, not verdicts
            # on the task. Deterministic shape failures are not retried.
            content = result["choices"][0]["message"]["content"]
            output = json.loads(_strip_code_fences(content))
            value = output["text"]
            if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
                raise _PermanentError()
        except _PermanentError:
            raise ValueError(
                f"Text helper returned no valid field value; nothing typed. Response: {content!r}"
            ) from None
        except RuntimeError as exc:
            if attempt >= 2 or not _request_retryable(exc):
                detail = f" Response: {content!r}" if content is not None else f" Request failed: {exc}"
                raise ValueError(f"Text helper returned no valid field value; nothing typed.{detail}") from None
            time.sleep(0.5 * 2**attempt)
        except (KeyError, IndexError, TypeError, ValueError):
            if attempt >= 2:
                raise ValueError(
                    f"Text helper returned no valid field value; nothing typed. Response: {content!r}"
                ) from None
            time.sleep(0.5 * 2**attempt)
        else:
            return value, {
                "model": model,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "usage": result.get("usage", {}),
            }
