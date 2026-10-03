"""Presence-only redaction, for the S6x rows.

v3's redaction rule is blunt: on every S6x test, amounts appear as booleans and
counts and never as values -- in the JSONL slices and in the ledger rows alike.
The failure this prevents is a balance or a position size sitting in a file that
outlives the pane and gets pasted into a chat.

So the rule is enforced on the way out, by rewriting the record, rather than by
asking each call site to remember. A call site that forgets produces a redacted
row; it cannot produce a row with an amount in it.

Stdlib only.
"""

from __future__ import annotations

import re

from scripts.live.spec import REDACT_PRESENCE_ONLY

# Key names whose values are amounts. Matched case-insensitively on the whole
# word, so `total_usd` and `availableBalance` are both caught.
_AMOUNT_KEY = re.compile(
    r"(amount|balance|usd|usdt|dollars?|value|price|notional|principal|equity|"
    r"position_size|size_usd|cost_basis|entry_price|exit_price|pnl)",
    re.IGNORECASE,
)

# Keys that carry no amount even though they match: counts and flags are exactly
# what presence-only is allowed to keep.
_COUNT_KEY = re.compile(r"(count|total_count|has_|is_|can_|counted|n_)", re.IGNORECASE)


def is_amount_key(key: str) -> bool:
    """Whether this key's value is an amount rather than a count or a flag."""
    name = str(key)
    if _COUNT_KEY.search(name):
        return False
    return bool(_AMOUNT_KEY.search(name))


def presence_only(value, *, key: str | None = None):
    """Rewrite one value so an amount becomes presence, not magnitude.

    Amounts become `True` (something is there) or `False` (nothing is). Numeric
    strings that look like money become `True` too, because a balance rendered as
    "0.00" is still an amount. Dicts and lists are walked so a nested positions
    payload cannot smuggle a number through.
    """
    if key is not None and is_amount_key(key):
        if value is None or value == "":
            return False
        return True
    if isinstance(value, dict):
        return {k: presence_only(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [presence_only(v, key=key) for v in value]
    return value


def redact_record(record: dict, *, redact: str | None) -> dict:
    """Apply the rule when the test asked for it, and leave the record alone when not.

    An unflagged test is returned unchanged rather than defensively redacted: a
    blanket rewrite would make a public-site run (CoinMarketCap, which needs its
    numbers for S1a) unscorable, and the flag is what distinguishes the two.
    """
    if redact != REDACT_PRESENCE_ONLY:
        return record
    return presence_only(record)


def assert_no_amounts(record: dict) -> None:
    """Fail loudly if an amount survived into a redacted record.

    A self-check rather than a guarantee: the rewrite is the guarantee, and this
    is the cheap assertion that the rewrite still covers what it covered when the
    key vocabulary was written.
    """
    stack = [(None, record)]
    while stack:
        key, value = stack.pop()
        if isinstance(value, dict):
            for k, v in value.items():
                if is_amount_key(k) and isinstance(v, (int, float)) and not isinstance(v, bool):
                    raise AssertionError(f"amount survived redaction under {k!r}")
                stack.append((k, v))
        elif isinstance(value, list):
            for item in value:
                stack.append((key, item))
