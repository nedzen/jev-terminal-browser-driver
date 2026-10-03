"""Enter as the no-button search submit, and the line it may not cross.

The action is dispatched blind (no node, no hit-test), so what decides safety is
which field it is offered on. Search boxes only, plus the original filled-field
rule. No browser, no network, no paid call.
"""

from jev_driver import model
from jev_driver.browser import _offer_enter
from jev_driver.questions import NEXT_ACTION

SEARCH = {"id": "s1", "kind": "fill", "node": 1, "role": "searchbox", "label": "Search", "value": ""}
EMPTY_TEXT = {"id": "t1", "kind": "fill", "node": 2, "role": "textbox", "label": "Comment", "value": ""}
FILLED_TEXT = {"id": "t2", "kind": "fill", "node": 3, "role": "textbox", "label": "Query", "value": "jev"}


def _page(*actions):
    return {"actions": [dict(action) for action in actions]}


def _ids(page):
    return [item["id"] for item in page["actions"]]


def _prompt_line():
    """The one NEXT_ACTION line that mentions PRESS_ENTER, or a loud failure if it is gone."""
    assert "PRESS_ENTER" in NEXT_ACTION, "the prompt no longer describes PRESS_ENTER"
    return next(item for item in NEXT_ACTION.splitlines() if "PRESS_ENTER" in item)


def test_a_search_box_gets_enter_with_no_text_typed():
    """The no-button search path: the field is empty and Enter is still on the table."""
    page = _page(SEARCH)

    _offer_enter(page)

    assert _ids(page) == ["s1", "press_enter"]


def test_an_empty_comment_box_does_not_get_enter():
    """The line. Enter on a comment or checkout field submits the form it belongs to."""
    page = _page(EMPTY_TEXT)

    _offer_enter(page)

    assert _ids(page) == ["t1"]


def test_a_textbox_named_search_is_not_treated_as_a_search_box():
    """`searchbox` comes from input[type=search], not from what the label claims."""
    page = _page({**EMPTY_TEXT, "label": "Search"})

    _offer_enter(page)

    assert _ids(page) == ["t1"]


def test_an_autocomplete_combobox_does_not_get_enter_on_being_empty():
    """A combobox's Enter selects a suggestion, which can carry a form with it."""
    page = _page({**EMPTY_TEXT, "role": "combobox"})

    _offer_enter(page)

    assert _ids(page) == ["t1"]


def test_a_filled_field_still_gets_enter_as_it_did_before():
    """Unchanged: a field the run already filled keeps the submit offer."""
    page = _page(FILLED_TEXT)

    _offer_enter(page)

    assert _ids(page) == ["t2", "press_enter"]


def test_enter_is_offered_once_per_observation():
    page = _page(SEARCH, FILLED_TEXT)

    _offer_enter(page)
    _offer_enter(page)

    assert _ids(page) == ["s1", "t2", "press_enter"]


def test_enter_is_a_control_and_not_a_per_operation_target():
    """Enter has no element index, so it is never a target choice for any operation."""
    page = _page(SEARCH)
    _offer_enter(page)

    _elements, targets, controls = model.action_space(page["actions"])

    assert sorted(controls) == ["PRESS_ENTER"]
    assert sorted(targets) == ["TYPE_TEXT"]  # only the search field itself is choosable
    assert "PRESS_ENTER" not in targets


def test_the_denylist_can_still_withdraw_enter():
    """The lever the audit relies on: a caller who distrusts Enter can deny it by name."""
    page = _page(SEARCH)
    _offer_enter(page)

    _elements, _targets, controls = model.action_space(page["actions"], deny_names=[r"^Press Enter$"])

    assert controls == {}


def test_the_prompt_names_press_enter_as_the_no_button_search_path():
    prompt = _prompt_line()

    assert "no Search or Submit button" in prompt
    assert "only searches" in prompt


def test_the_prompt_scopes_enter_to_a_search_field():
    """The instruction must not read as a general submit hint for any form."""
    assert "search field" in _prompt_line()
    assert "comment" not in NEXT_ACTION.lower()
