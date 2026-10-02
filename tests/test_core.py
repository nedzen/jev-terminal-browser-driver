"""plugin/core: the stdlib-only leaf both sides of the boundary share.

Three properties, each with a test that bites:
- core imports nothing outside the standard library and plugin/ (AST walk), so
  Hermes can load it without jev_driver and jev_driver can import it.
- There is ONE row/result field table, so a field declared there survives the
  round trip on its own rather than needing a hand-written copy in two places.
- cli.tick_record and cli.trace_fields each assemble through one builder, not
  a second literal dict.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from jev_driver import cli
from plugin.core import result
from plugin.core.result import Field, build_tick_row, compact_result

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "plugin" / "core"


def test_core_imports_nothing_outside_stdlib_and_plugin():
    """Hermes loads plugin/ without jev_driver; this is what keeps that true.

    An import of jev_driver here would make the Hermes gate unimportable, and an
    import of a third-party package would make it unimportable at all.
    """
    allowed = sys.stdlib_module_names | {"plugin"}
    offenders = []
    for path in sorted(CORE.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import, which cannot leave plugin/core.
                if node.level:
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] not in allowed:
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_the_wheel_ships_the_packages_the_console_script_imports():
    """`wwwdrive` is `jev_driver.cli:main`, and cli imports plugin.core.result.
    A wheel packaging only jev_driver installs a console script that cannot
    start — a failure at the entry point, not at import of an unused module.
    """
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert set(packages) == {"jev_driver", "plugin"}


def test_the_driver_needs_no_third_party_to_build_a_row():
    """cli imports the builder, so the builder's cost is the driver's cost."""
    tree = ast.parse((ROOT / "jev_driver" / "cli.py").read_text())
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("plugin")
    }
    assert imported == {"plugin.core.result", "plugin.core.trace"}


def test_a_novel_declared_field_survives_the_round_trip(monkeypatch):
    """The dropped-field class: `final_view` and `omitted_actions` both nearly
    died because a field added to the row was never added to the fold.

    So the fold is driven by the declaration. A field nobody has written a case
    for must arrive, having touched only the table.
    """
    novel = Field("shelf_note", truthy=True)
    monkeypatch.setattr(result, "PASSTHROUGH", result.PASSTHROUGH + (novel,))
    monkeypatch.setattr(result, "TICK_OPTIONAL", result.TICK_OPTIONAL + (novel,))

    snap = {
        "status": "done",
        "page": {"url": "https://example.test/x", "text": "The order was confirmed."},
        "history": [],
        "decisions": [{"choice": "DONE", "operation": "DONE", "confidence": 0.8}],
    }
    # Driver side: cli assembles the row, and the field rides out as an override.
    rec = cli.tick_record(snap, shelf_note="left on the top shelf")
    assert rec["shelf_note"] == "left on the top shelf"

    # The wire, then the agent side: an undeclared fold would drop it here.
    rows = result.parse_json_lines(json.dumps(rec))
    out = compact_result(rows, 0)
    assert out["shelf_note"] == "left on the top shelf"
    assert out["status"] == "done"


def test_tick_record_assembles_through_the_builder(monkeypatch):
    """One assembly path. A second dict literal in cli.py is the bug this pins."""
    seen = []
    real = cli.build_tick_row

    def spy(base, **fields):
        seen.append(dict(base))
        return real(base, **fields)

    monkeypatch.setattr(cli, "build_tick_row", spy)
    snap = {"status": "done", "page": {"url": "https://example.test/x", "text": "hi"}, "history": []}
    cli.tick_record(snap)
    cli.tick_record(snap, status="blocked", error="max-steps", reason="max_steps", why="out of steps")
    cli.tick_record(snap, final_view={"url": "https://example.test/x"})
    assert [row["status"] for row in seen] == ["done", "blocked", "done"]
    assert seen[1]["status"] == "blocked"  # the override is the row's status, not a post-hoc patch


def test_an_override_of_none_leaves_the_snapshot_value_alone():
    """The tick loop always passes `error`/`why`/`final_view`; a run that did
    not stop must keep the why the agent computed."""
    snap = {
        "status": "ready",
        "page": {"url": "https://example.test/x"},
        "history": [{"action": "Widget", "operation": "CLICK", "page_changed": True}],
        "decisions": [],
    }
    rec = cli.tick_record(snap, error=None, why=None, final_view=None)
    assert "error" not in rec and "final_view" not in rec
    assert rec["why"] == "CLICK Widget (page changed)"


def test_page_text_is_capped_by_the_declared_limit_not_a_literal(monkeypatch):
    """One limit, declared once. cli used to carry its own `[:2000]`."""
    monkeypatch.setattr(result, "PAGE_TEXT_LIMIT", 10)
    short = result.Field("page_text", limit=10)
    monkeypatch.setattr(result, "PASSTHROUGH", (short,) + tuple(f for f in result.PASSTHROUGH if f.name != "page_text"))
    monkeypatch.setattr(
        result,
        "TICK_OPTIONAL",
        (short,) + tuple(f for f in result.TICK_OPTIONAL if f.name != "page_text"),
    )
    snap = {"status": "done", "page": {"url": "u", "text": "y" * 5000}, "history": []}
    rec = cli.tick_record(snap)
    assert rec["page_text"] == "y" * 10
    assert compact_result([rec], 0)["page_text"] == "y" * 10


def test_an_undeclared_field_is_refused_by_the_builder():
    """A row must not grow a key compact_result has no rule for."""
    rec = build_tick_row({"status": "ready"}, mystery=1)
    assert "mystery" not in rec


def test_an_empty_value_is_absent_where_blank_means_nothing():
    """`why: ""` in the row would overwrite the run's own why on the way in."""
    assert "why" not in build_tick_row({"status": "ready"}, why="")
    assert "omitted_actions" not in build_tick_row({"status": "ready"}, omitted_actions=0)
    # A field that can legitimately be falsey keeps its None-vs-value rule.
    assert build_tick_row({"status": "ready"}, final_view={})["final_view"] == {}


def test_the_result_folds_the_run_not_the_last_row():
    """degenerate and insights aggregate over ticks, so they are not passthrough."""
    rows = [
        {"status": "ready", "degenerate": True, "insight": {"operation": "SCROLL_DOWN"}},
        {"status": "done", "url": "u"},
    ]
    out = compact_result(rows, 0)
    assert out["degenerate"] is True
    assert out["insights"] == [{"operation": "SCROLL_DOWN"}]
    assert compact_result(rows, 0, insights=False).get("insights") is None


@pytest.mark.parametrize("field", result.PASSTHROUGH, ids=lambda f: f.name)
def test_every_declared_field_is_carried_by_the_fold(field):
    """The table and the fold cannot disagree: same module, same loop."""
    sample = {
        "page_text": "text",
        "why": "because",
        "reason": "max_steps",
        "final_view": {"url": "u"},
        "omitted_actions": 37,
    }[field.name]
    rec = build_tick_row({"status": "done", "url": "u"}, **{field.name: sample})
    assert compact_result([rec], 0)[field.name] == field.cap(sample)