"""deny_names: the plugin's denylist, from schema to the action space.

No Hermes, no browser, no paid call.
"""

import json
from unittest.mock import Mock

import pytest

import plugin
from jev_driver import cli, model
from plugin import handler


def page():
    """One denied control, one denied element, and two that must survive."""
    return {
        "url": "https://example.test/post/1",
        "title": "Post",
        "text": "Post body",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "click", "label": "Follow Ada", "role": "button", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Save draft", "role": "button", "value": "", "node": 20},
            {"id": "e3", "kind": "fill", "label": "Note", "role": "textbox", "value": "", "node": 30},
            {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
            {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
        ],
    }


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode

    def communicate(self, timeout=None):
        return self.stdout, ""

    def poll(self):
        return self.returncode


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "drive.py").write_text("# drive\n")
    monkeypatch.setenv("WWWDRIVE_HOME", str(tmp_path))
    monkeypatch.setattr(handler, "LOG_DIR", tmp_path / "logs")
    return tmp_path


@pytest.fixture(autouse=True)
def no_denylist(monkeypatch):
    """Every test starts with an empty denylist and leaves the module global as it found it."""
    monkeypatch.setattr(model, "DENY_NAMES", ())


def test_drive_schema_offers_deny_names_as_an_optional_regex_list():
    prop = plugin.SCHEMA["parameters"]["properties"]["deny_names"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "string"}
    assert "deny_names" not in plugin.SCHEMA["parameters"]["required"]
    # read never drives, so it never offers a denylist.
    assert "deny_names" not in plugin.READ_SCHEMA["parameters"]["properties"]
    # The knob is discoverable: tool_search reads the description, not the schema.
    assert "deny_names" in plugin.DESCRIPTION


def test_valid_patterns_reach_the_driver_argv(home):
    captured = {}

    def popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc(stdout=json.dumps({"status": "done", "url": "x"}), returncode=0)

    handler.run_drive({"goal": "g", "deny_names": ["^Follow", "log ?out"]}, popen=popen)
    argv = captured["argv"]
    assert [argv[i + 1] for i, token in enumerate(argv) if token == "--deny-name"] == ["^Follow", "log ?out"]


def test_no_denylist_adds_no_flags(home):
    captured = {}

    def popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc(stdout=json.dumps({"status": "done", "url": "x"}), returncode=0)

    handler.run_drive({"goal": "g"}, popen=popen)
    assert "--deny-name" not in captured["argv"]


def test_an_uncompilable_pattern_is_rejected_before_the_spawn(home):
    spawned = []

    def popen(argv, **kwargs):
        spawned.append(argv)
        return FakeProc(stdout=json.dumps({"status": "done", "url": "x"}), returncode=0)

    out = handler.run_drive({"goal": "g", "deny_names": ["Follow ("]}, popen=popen)
    assert spawned == []
    assert "not a valid regular expression" in out["error"]
    assert out["status"] == "error"


@pytest.mark.parametrize("value", ["^Follow", ["ok", 7], ["ok", None], ["ok", ""], ["ok", "   "], 42])
def test_a_malformed_denylist_is_rejected_before_the_spawn(home, value):
    spawned = []

    def popen(argv, **kwargs):
        spawned.append(argv)
        return FakeProc(stdout=json.dumps({"status": "done", "url": "x"}), returncode=0)

    out = handler.run_drive({"goal": "g", "deny_names": value}, popen=popen)
    assert spawned == []
    assert "deny_names must be a list of regular expressions" in out["error"]


def test_the_denied_element_never_reaches_the_action_space():
    elements, targets, controls = model.action_space(page()["actions"], deny_names=["^Follow"])
    # Dropped before indexing, so the survivor keeps index 1: no gap to read past.
    assert [element["label"] for element in elements] == ["Save draft", "Note"]
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["TYPE_TEXT"]["2"]["id"] == "e3"
    assert set(controls) == {"SCROLL_DOWN", "WAIT"}


def test_a_denied_control_is_not_offered_as_an_operation():
    _elements, targets, controls = model.action_space(page()["actions"], deny_names=["^Scroll"])
    assert "SCROLL_DOWN" not in controls
    assert "SCROLL_DOWN" not in targets


def test_the_run_denylist_applies_without_being_passed_again(monkeypatch):
    """cli.main installs the denylist once; choose() reads it without a per-call argument."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        ids = list(body["questions"]["operation"]["criteria"])
        picked = ids[0]
        probabilities = {i: float(i == picked) for i in ids}
        return {
            "model": "test",
            "answers": {"operation": {"choice": picked, "confidence": 1.0, "probabilities": probabilities}},
        }

    monkeypatch.setattr(model, "post_json", post)
    model.set_deny_names(["^Follow"])
    decision = model.choose(page(), "Save the draft", [])
    assert decision["operation"] != "SCROLL_DOWN"
    body = json.dumps(calls[0])
    assert "Follow Ada" not in body  # not offered at all, not merely unindexed
    assert "Save draft" in body  # the rest of the page is untouched


def test_an_uncompilable_pattern_never_reaches_a_run():
    with pytest.raises(ValueError, match="not a valid regular expression"):
        model.set_deny_names(["Follow ("])
    assert model.DENY_NAMES == ()


def test_the_cli_collects_repeated_deny_name_flags():
    args = cli.parse_args(["--goal", "g", "--deny-name", "^Follow", "--deny-name", "log ?out"])
    assert args.deny_names == ["^Follow", "log ?out"]
    assert cli.parse_args(["--goal", "g"]).deny_names is None


def test_a_bad_pattern_stops_the_run_before_a_browser_is_opened(monkeypatch, capsys):
    """The handler already refuses these, so the CLI must too: the CLI is reachable on its own."""
    monkeypatch.setattr(cli, "discover", Mock(side_effect=AssertionError("must not discover")))
    assert cli.main(["--goal", "g", "--deny-name", "Follow ("]) == 1
    assert "not a valid regular expression" in capsys.readouterr().err
