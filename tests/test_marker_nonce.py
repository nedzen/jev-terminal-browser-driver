"""B-cycle: the driver's own `#jev=` marker must not read as page staleness.

`_unique_url` mints `#jev=<time_ns()>` on every new tab so a re-opened tab is a
distinct URL in CDP's target list. `marker[1]` and `page_key[1]` are both
`location.href` (snapshot.js:98, snapshot.js:45), and both were compared
verbatim — so a difference the *driver itself* authored, with an unchanged
`performance.timeOrigin` and an unchanged page, was reported as
`target_changed`, which `act()` reports to the caller as `field_changed`.

The recorded symptom: 2026-10-02T22:29 and 22:41, example.com, `click_not_sent`
after two `field_changed` on the Learn-more link. Frozen payloads below carry the
hrefs from those traces. example.com is retired from new live runs, so nothing
here drives a browser.
"""

import pytest

from jev_driver.browser import (
    Browser,
    _same_document,
    _same_href,
    _same_marker,
    _same_target,
    _strip_jev_marker,
    _unique_url,
)

# Verbatim from the 22:29/22:41 traces: the nonce the driver minted, and the bare
# href the same run recorded at `run` time.
NONCE_URL = "https://example.com/#jev=1790980167661144000"
BARE_URL = "https://example.com/"
SITE = "https://www.iana.org/domains/example"

# snapshot.js pageKey: [timeOrigin, href, scrollX, scrollY, innerW, innerH, fields]
def page_key(href, time_origin=1000.0):
    return [time_origin, href, 0, 0, 1280, 720, []]


# snapshot.js marker: [timeOrigin, href, scrollX, scrollY, innerW, innerH,
#                      title, text, semantics, page_key[6]]
def marker(href, time_origin=1000.0, text="body", semantics=()):
    return [time_origin, href, 0, 0, 1280, 720, "Example Domain", text, list(semantics), []]


# --------------------------------------------------------------------------
# The defect: the driver's own nonce reads as navigation
# --------------------------------------------------------------------------


def test_the_marker_the_driver_minted_is_not_staleness():
    """Same timeOrigin, same page, same everything — only the driver's own nonce
    differs, and that must not read as a changed page."""
    assert _same_marker(marker(NONCE_URL), marker(BARE_URL)) is True


def test_two_nonces_for_the_same_page_are_the_same_page():
    """`_unique_url` is not idempotent, so a run that re-opens its tab records two
    different nonces for one page. Both are the driver's; neither is the site's."""
    assert _same_marker(marker("https://example.com/#jev=111"), marker("https://example.com/#jev=999")) is True


def test_the_click_path_agrees_with_the_marker_path():
    """The click path reaches `_same_document` through the probe rather than
    through `MARKER`, so it needs the same answer or one path still refuses."""
    stored = {"page_key": page_key(NONCE_URL)}
    assert _same_document(stored, page_key(BARE_URL)) is True


def test_act_reports_no_field_changed_for_a_nonce_only_difference():
    """The user-visible symptom through the branch that reads `MARKER`.

    `fresh()` has three branches and only the last compares the whole marker:
    click/select/fill go to the probe, scroll/wait/done to `_same_document`. The
    kind here must fall outside all three, or this test passes without ever
    touching the code it names.
    """
    browser = Browser.__new__(Browser)
    browser._fresh_reason = None
    browser._probe_detail = {}
    browser.evaluate = lambda expression: marker(BARE_URL)
    page = {"marker": marker(NONCE_URL), "page_key": page_key(NONCE_URL)}

    assert browser.fresh(page, {"kind": "hover"}) is True
    assert browser._fresh_reason == "ok"


def test_the_wait_branch_is_also_unaffected():
    """scroll/wait/done take the `_same_document` branch instead, and the recorded
    runs stopped on a click, so both paths are pinned rather than one assumed."""
    browser = Browser.__new__(Browser)
    browser._fresh_reason = None
    browser._probe_detail = {}
    browser.evaluate = lambda expression: [1000.0, BARE_URL]
    page = {"marker": marker(NONCE_URL), "page_key": page_key(NONCE_URL)}

    assert browser.fresh(page, {"kind": "wait"}) is True
    assert browser._fresh_reason == "ok"


def test_the_marker_branch_still_refuses_a_real_change():
    """...and the branch that ignores the nonce still catches a page that moved."""
    browser = Browser.__new__(Browser)
    browser._fresh_reason = None
    browser._probe_detail = {}
    browser.evaluate = lambda expression: marker(BARE_URL, time_origin=2000.0)
    page = {"marker": marker(NONCE_URL), "page_key": page_key(NONCE_URL)}

    assert browser.fresh(page, {"kind": "hover"}) is False
    assert browser._fresh_reason == "target_changed"


def test_a_target_is_still_the_same_target_across_nonces():
    """Identity is checked after the document check, so both had to agree."""
    guard = [{"role": "link"}, SITE]
    stored = {"page_key": page_key(NONCE_URL), "guards": {"7": guard}}
    current = [page_key(BARE_URL), guard, [True, True, True, True, True]]
    assert _same_target(stored, 7, current) is True


# --------------------------------------------------------------------------
# A real change is still a real change
# --------------------------------------------------------------------------


def test_a_real_navigation_is_still_stale():
    """timeOrigin changes when the document does. That is the signal, and it must
    keep working: the nonce is the driver's noise, this is the site's."""
    assert _same_marker(marker(NONCE_URL), marker(BARE_URL, time_origin=2000.0)) is False
    assert _same_document({"page_key": page_key(NONCE_URL)}, page_key(BARE_URL, time_origin=2000.0)) is False


def test_changed_text_is_still_stale():
    """The marker exists to catch a page that re-rendered under the decision."""
    assert _same_marker(marker(NONCE_URL, text="before"), marker(NONCE_URL, text="after")) is False


def test_changed_action_semantics_are_still_stale():
    """A re-rendered page with different controls reads as changed even when the
    text and the href are identical."""
    assert _same_marker(marker(NONCE_URL, semantics=["a"]), marker(NONCE_URL, semantics=["b"])) is False


def test_a_site_fragment_is_still_navigation():
    """Only the driver's `jev=<digits>` is dropped. `#section` is the site moving,
    and refusing a decision because of it is correct."""
    assert _same_href("https://x.test/#one", "https://x.test/#two") is False
    assert _same_href("https://x.test/#/route/1", "https://x.test/#/route/2") is False
    assert _same_href("https://x.test/#section", "https://x.test/") is False


# --------------------------------------------------------------------------
# The strip is narrow
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        (NONCE_URL, BARE_URL),
        (BARE_URL, BARE_URL),
        ("https://x.test/#jev=1", "https://x.test/"),
        # another fragment parameter survives beside it
        ("https://x.test/#jev=1&tab=2", "https://x.test/#tab=2"),
        ("https://x.test/#tab=2&jev=1", "https://x.test/#tab=2"),
        # not the driver's shape: left alone
        ("https://x.test/#jev=abc", "https://x.test/#jev=abc"),
        ("https://x.test/#notjev=1", "https://x.test/#notjev=1"),
        ("https://x.test/#jev=1x", "https://x.test/#jev=1x"),
        # a jev= in the query or the path is not the fragment marker
        ("https://x.test/?jev=1", "https://x.test/?jev=1"),
        ("https://x.test/jev=1", "https://x.test/jev=1"),
        # non-strings pass through untouched rather than raising
        (None, None),
    ],
)
def test_the_strip_removes_only_the_drivers_own_parameter(href, expected):
    assert _strip_jev_marker(href) == expected


def test_a_bare_hash_fragment_is_untouched():
    """A trailing `#` is the site being where it is, not a nonce to drop."""
    assert _strip_jev_marker("https://x.test/#") == "https://x.test/#"


def test_unique_url_still_mints_a_marker():
    """The nonce is load-bearing for CDP's target list, so it must still be minted
    — the fix is in the comparison, not in the writer.

    Only the shape is asserted, not distinctness: `time.time_ns()` can collide
    between two calls in the same tick, and that has never been load-bearing.
    """
    minted = _unique_url(BARE_URL)
    assert "#jev=" in minted
    assert minted.split("#", 1)[0] == BARE_URL
    # …and what it mints is exactly what the strip recognises.
    assert _strip_jev_marker(minted) == BARE_URL


def test_a_minted_marker_reads_as_the_page_it_opened():
    """The round trip that matters: whatever nonce `_unique_url` produces must
    compare equal to the same URL without it — including when the site brought its
    own fragment, which the nonce is appended to rather than replacing."""
    assert _same_href(_unique_url(BARE_URL), BARE_URL) is True
    assert _same_href(_unique_url(BARE_URL), NONCE_URL) is True
    # `#anchor` is the site's, so it survives the strip and still distinguishes.
    assert _strip_jev_marker(_unique_url("https://x.test/page#anchor")) == "https://x.test/page#anchor"
    assert _same_href(_unique_url("https://x.test/page#anchor"), "https://x.test/page#anchor") is True
    assert _same_href(_unique_url("https://x.test/page#anchor"), "https://x.test/page#other") is False


def test_a_marker_of_the_wrong_shape_is_compared_as_is():
    """Both sides malformed, or one of them: fall back to equality rather than
    raising or silently accepting."""
    assert _same_marker(None, None) is True
    assert _same_marker(None, marker(NONCE_URL)) is False
    assert _same_marker(marker(NONCE_URL), None) is False
    assert _same_marker([1.0], [1.0, "href"]) is False
    assert _same_marker({"a": 1}, {"a": 1}) is True