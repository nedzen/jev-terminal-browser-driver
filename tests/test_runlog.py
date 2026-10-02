"""Tab reuse decision and the on-disk run log. No live browser."""

import json

from jev_driver.cli import choose_lease, no_page_error, trace_fields
from jev_driver.runlog import write_event


def test_url_navigates_the_existing_driver_tab():
    plan = choose_lease(
        url="https://example.test/next",
        target_id=None,
        continuable=("T1", "https://example.test/now"),
        default_url="file:///fixture",
    )
    assert plan["tab"] == "target"
    assert plan["target_id"] == "T1"
    assert plan["navigate"] is True
    assert plan["agent_url"] == "https://example.test/next"
    assert plan["continuity"] == "re-attach"


def test_missing_tab_is_the_only_new_tab():
    plan = choose_lease(
        url="https://example.test/next",
        target_id=None,
        continuable=(None, None),
        default_url="file:///fixture",
        dropped="dropped:stale-id",
    )
    assert plan["tab"] == "new"
    assert plan["navigate"] is True
    assert plan["agent_url"] == "https://example.test/next"


def test_same_url_does_not_reload_the_existing_tab():
    plan = choose_lease(
        url="https://x.com/i/history/likes",
        target_id=None,
        continuable=("T1", "https://x.com/i/history/likes/"),
        default_url="file:///fixture",
    )
    assert plan["navigate"] is False
    assert plan["continuity"] == "re-attach"
    assert plan["agent_url"] == "https://x.com/i/history/likes"


def test_omit_url_stays_on_the_same_tab_without_navigating():
    plan = choose_lease(
        url=None,
        target_id=None,
        continuable=("T1", "https://example.test/now"),
        default_url="file:///fixture",
    )
    assert plan["tab"] == "target"
    assert plan["navigate"] is False
    assert plan["agent_url"] == "https://example.test/now"


def test_explicit_target_is_not_replaced_by_the_remembered_tab():
    plan = choose_lease(
        url="https://example.test/next",
        target_id="EXPLICIT",
        continuable=("T1", "https://example.test/now"),
        default_url="file:///fixture",
        navigate_explicit=False,
    )
    assert plan["target_id"] == "EXPLICIT"
    assert plan["navigate"] is False


def test_blocked_trace_keeps_goal_and_ranked_hits():
    snap = {
        "decisions": [
            {
                "operation": "CLICK",
                "operation_probabilities": {"CLICK": 0.42, "BLOCKED": 0.4, "SCROLL_DOWN": 0.18},
                "target_probabilities": {"1": 0.55, "2": 0.45},
                "request": {
                    "questions": {
                        "click_target": {
                            "criteria": {"1": "[1] Widget; link", "2": "[2] Unrelated; link"}
                        }
                    }
                },
            }
        ]
    }
    rec = {
        "status": "blocked",
        "url": "https://example.test/",
        "last_action": "Widget",
        "why": "Stopped: three actions in a row left the page unchanged.",
        "page_text": "Home",
    }
    fields = trace_fields(snap, rec, goal="Click the Widget link")
    assert fields["event"] == "blocked"
    assert fields["goal"] == "Click the Widget link"
    assert fields["ranked_ops"][0] == {"name": "CLICK", "p": 0.42}
    assert fields["ranked_targets"][0] == {"name": "Widget", "p": 0.55}
    assert fields["ranked_targets"][1]["name"] == "Unrelated"


def test_log_writes_goal_ranks_and_block(tmp_path):
    jsonl = tmp_path / "drive.jsonl"
    text = tmp_path / "drive.log"
    write_event(
        {
            "event": "run",
            "goal": "Click the Widget link",
            "url": "https://example.test/",
            "continuity": "re-attach",
        },
        jsonl_path=jsonl,
        text_path=text,
    )
    write_event(
        {
            "event": "blocked",
            "goal": "Click the Widget link",
            "status": "blocked",
            "why": "Model chose BLOCKED: no supported operation can progress this goal.",
            "url": "https://example.test/stuck",
            "ranked_ops": [{"name": "BLOCKED", "p": 0.7}, {"name": "CLICK", "p": 0.3}],
            "ranked_targets": [{"name": "Widget", "p": 0.4}],
            "page_text": "nothing useful here",
        },
        jsonl_path=jsonl,
        text_path=text,
    )
    rows = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert rows[0]["goal"] == "Click the Widget link"
    assert rows[0]["continuity"] == "re-attach"
    assert rows[1]["event"] == "blocked"
    assert rows[1]["ranked_ops"][0]["name"] == "BLOCKED"
    body = text.read_text()
    assert "Click the Widget link" in body
    assert "BLOCKED 0.7" in body
    assert "Widget 0.4" in body
    assert "nothing useful here" in body
    assert "continuity=re-attach" in body


def test_no_url_and_no_tab_blocks_instead_of_opening_the_fixture():
    plan = choose_lease(
        url=None,
        target_id=None,
        continuable=(None, None),
        default_url="file:///fixture",
        dropped="dropped:stale-id",
    )
    error = no_page_error(plan, None)
    assert error and "dropped:stale-id" in error
    assert no_page_error(plan, "https://example.test/") is None


def test_cli_reads_the_live_continuity_reason():
    import jev_driver.cli as cli

    assert not hasattr(cli, "LAST_CONTINUITY")


# --- run log hardening: redaction, size caps, never raises ----------------
# (upstream PR browser-use/jev-ultrafast#141)


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_credentials_never_reach_either_log_file(tmp_path):
    jsonl = tmp_path / "drive.jsonl"
    text = tmp_path / "drive.log"
    write_event(
        {
            "event": "run",
            "goal": "Sign in",
            "url": "https://x.test/?api_key=sk-live-AAAA1111&next=/home",
            "API_KEY": "sk-live-BBBB2222",
            "password": "hunter2-shhh",
            "Authorization": "Bearer ghp_secret_token_value",
            "cookie": "session=abc123def",
            "tokens": ["sk-live-EEEE5555"],
            "nested": {"notes": "refresh_token: ghp_secret_token_value", "input_tokens": 12},
        },
        jsonl_path=jsonl,
        text_path=text,
    )
    row = rows(jsonl)[0]
    blob = json.dumps(row) + text.read_text()
    secrets = (
        "sk-live-AAAA1111",
        "sk-live-BBBB2222",
        "hunter2-shhh",
        "ghp_secret_token_value",
        "abc123def",
        "sk-live-EEEE5555",
    )
    for secret in secrets:
        assert secret not in blob, secret
    for key in ("API_KEY", "password", "Authorization", "cookie", "tokens"):
        assert row[key] == "[redacted]", key
    assert row["nested"]["input_tokens"] == 12  # token *counts* are not secrets
    assert "ghp_secret_token_value" not in row["nested"]["notes"]
    assert "/home" in row["url"]  # the harmless part of the url survives
    assert "redacted" in row["nested"]["notes"]


def test_bearer_and_assignment_values_are_redacted_in_free_text(tmp_path):
    jsonl = tmp_path / "drive.jsonl"
    text = tmp_path / "drive.log"
    write_event(
        {
            "event": "blocked",
            "why": "cdp replied: Authorization: Bearer sk-live-CCCC3333 then token=gHP_secret_2 rejected",
            "error": "connect failed using Bearer eyJhbGciOi.J9.sig",
            "goal": "retry with password: hunter2-shhh",
        },
        jsonl_path=jsonl,
        text_path=text,
    )
    row = rows(jsonl)[0]
    blob = json.dumps(row) + text.read_text()
    for secret in ("sk-live-CCCC3333", "gHP_secret_2", "eyJhbGciOi.J9.sig", "hunter2-shhh"):
        assert secret not in blob, secret
    assert "Authorization" in row["why"]  # the fact stays, the value does not
    assert "redacted" in row["why"]


def test_oversized_values_are_capped(tmp_path):
    jsonl = tmp_path / "drive.jsonl"
    text = tmp_path / "drive.log"
    write_event(
        {
            "event": "blocked",
            "why": "x" * 5000,
            "page_text": "y" * 900,
            "ranked_ops": [{"name": f"OP{i}", "p": 0.1} for i in range(50)],
            "ranked_targets": [{"name": f"T{i}", "p": 0.2} for i in range(25)],
        },
        jsonl_path=jsonl,
        text_path=text,
    )
    row = rows(jsonl)[0]
    assert len(row["why"]) == 200 and row["why"].endswith("…")
    assert len(row["page_text"]) == 200
    assert len(row["ranked_ops"]) == 20
    assert len(row["ranked_targets"]) == 20
    assert row["ranked_ops"][0]["name"] == "OP0"
    for line in text.read_text().splitlines():
        assert len(line) < 1000  # the readable log stays bounded too


def test_write_event_never_raises_on_hostile_input(tmp_path):
    jsonl = tmp_path / "drive.jsonl"
    text = tmp_path / "drive.log"
    circular = {"event": "loop"}
    circular["self"] = circular
    hostile = [
        None,
        "just a string",
        42,
        [1, 2, 3],
        {"a", "b"},
        object(),
        b"\x00" * 10_000,
        circular,
        {"deep": {"deep": {"deep": {"deep": {"deep": {"deep": {"deep": "bottom"}}}}}}},
        {1: "non-string key", None: "null key"},
        {"ranked_ops": "not a list of dicts"},
    ]
    for event in hostile:
        write_event(event, jsonl_path=jsonl, text_path=text)
    written = rows(jsonl)
    assert len(written) == len(hostile)
    blob = json.dumps(written)
    assert "[circular]" in blob
    assert "[truncated]" in blob
    assert "just a string" in blob  # still legible, still capped
    assert text.read_text().strip()


def test_write_event_is_idempotent_and_leaves_the_event_alone(tmp_path):
    jsonl = tmp_path / "drive.jsonl"
    text = tmp_path / "drive.log"
    event = {"event": "run", "goal": "g", "url": "https://x.test/?api_key=sk-live-DDDD6666", "notes": ["a"] * 30}
    before = json.dumps(event, sort_keys=True)
    write_event(event, jsonl_path=jsonl, text_path=text)
    write_event(event, jsonl_path=jsonl, text_path=text)
    assert json.dumps(event, sort_keys=True) == before
    first, second = rows(jsonl)
    first.pop("ts"), second.pop("ts")
    assert first == second
    assert len(first["notes"]) == 20
    assert "sk-live-DDDD6666" not in text.read_text()


def test_write_event_swallows_disk_errors(tmp_path):
    blocked = tmp_path / "a-directory-not-a-log"
    blocked.mkdir()
    write_event({"event": "run"}, jsonl_path=blocked, text_path=blocked)  # must not raise
    assert blocked.is_dir()


def test_quoted_and_prefixed_credentials_are_redacted(tmp_path):
    """Every shape a credential turns up in: quoted key, prefixed key, scheme.

    Each of these reached drive.jsonl verbatim before the assignment key group
    gained its optional quote and one `prefix_` segment.
    """
    jsonl = tmp_path / "drive.jsonl"
    text = tmp_path / "drive.log"
    leaks = [
        ("why", 'server said {"password": "hunter2-json"}', "hunter2-json"),
        ("why", "AUTH_TOKEN: sk-live-LEAK9", "sk-live-LEAK9"),
        ("why", "my_password=hunter2-plain", "hunter2-plain"),
        ("why", "Authorization: Bearer sk-live-OK1", "sk-live-OK1"),
        ("why", "https://x.test/?api_key=sk-live-OK2&next=/home", "sk-live-OK2"),
        ("why", '{"token": "ghp_JSONQUOTED"}', "ghp_JSONQUOTED"),
        ("error", "SESSION_ID: abc123def", "abc123def"),
        ("why", "Set-Cookie: sid=cookievalue", "cookievalue"),
        ("why", "basic YWxhZGRpbjpvcGVuc2VzYW1l", "YWxhZGRpbjpvcGVuc2VzYW1l"),
    ]
    for field, payload, _secret in leaks:
        write_event({"event": "blocked", field: payload}, jsonl_path=jsonl, text_path=text)
    blob = json.dumps(rows(jsonl)) + text.read_text()
    for _field, _payload, secret in leaks:
        assert secret not in blob, secret
    assert blob.count("[redacted]") >= len(leaks)
    # the harmless surroundings survive, so the log stays useful
    assert "&next=/home" in blob
    assert "Authorization" in blob


def test_prefixed_secret_keys_are_redacted_whole(tmp_path):
    jsonl = tmp_path / "drive.jsonl"
    write_event(
        {
            "event": "run",
            "my_password": "hunter2-plain",
            "AUTH_TOKEN": "sk-live-LEAK10",
            "session_id": "abc123def",
            "apiKey": "sk-live-LEAK11",
            "cookies": "sid=abc",
            "nested": {"private_key": "-----BEGIN PRIVATE KEY-----", "input_tokens": 7},
        },
        jsonl_path=jsonl,
        text_path=tmp_path / "drive.log",
    )
    row = rows(jsonl)[0]
    for key in ("my_password", "AUTH_TOKEN", "session_id", "apiKey", "cookies"):
        assert row[key] == "[redacted]", key
    assert row["nested"]["private_key"] == "[redacted]"
    assert row["nested"]["input_tokens"] == 7  # a count, not a credential
    assert "hunter2-plain" not in json.dumps(row)


def test_benign_wording_with_credential_words_survives(tmp_path):
    """The other half of the contract: redaction must not eat the log."""
    jsonl = tmp_path / "drive.jsonl"
    survivors = [
        "goal: click the token next to Done",
        "why: input_tokens=1234 tokens=9876 counted",
        "label: author: Ada Lovelace",
        "why: bypassed the cache",
        "why: elapsed_ms: 900 then blocked",
    ]
    for payload in survivors:
        write_event({"event": "tick", "why": payload}, jsonl_path=jsonl, text_path=tmp_path / "drive.log")
    written = rows(jsonl)
    assert [row["why"] for row in written] == survivors


def test_one_exploding_key_does_not_cost_the_record(tmp_path):
    jsonl = tmp_path / "drive.jsonl"
    text = tmp_path / "drive.log"

    class ExplodingKey:
        def __str__(self):
            raise RuntimeError("str() exploded")

        def __hash__(self):
            return 7

    write_event(
        {"event": "blocked", "why": "kept anyway", ExplodingKey(): 1, "url": "https://x.test/"},
        jsonl_path=jsonl,
        text_path=text,
    )
    row = rows(jsonl)[0]
    assert row["why"] == "kept anyway"
    assert row["url"] == "https://x.test/"
    assert any("unserializable" in key for key in row)
    assert "kept anyway" in text.read_text()


def test_lone_surrogate_does_not_erase_the_record(tmp_path):
    jsonl = tmp_path / "drive.jsonl"
    text = tmp_path / "drive.log"
    write_event(
        {"event": "blocked", "why": "bad \ud800 char on the page", "url": "https://x.test/"},
        jsonl_path=jsonl,
        text_path=text,
    )
    row = rows(jsonl)[0]
    assert row["url"] == "https://x.test/"
    assert "bad" in row["why"] and "char on the page" in row["why"]
    assert row["ts"]  # on disk, not silently dropped
    assert text.read_text().strip()
