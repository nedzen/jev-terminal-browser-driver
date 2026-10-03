"""Startup preflight: shape, secrecy, --check, and the MCP status tool.

No browser, no model call, no paid anything: these checks are exactly what runs
when nothing is installed, so that is the state under test.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jev_driver import preflight as pf
from jev_driver.cli import main, parse_args
from plugin import STATUS_PARAMETERS, handler

ROOT = Path(__file__).resolve().parents[1]

KEY_VARS = ("DECISION_GATE_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY")


@pytest.fixture()
def mcp_mod(load_mcp):
    return load_mcp("wwwdrive_mcp_preflight")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No ambient key, no ambient checkout, no ambient binary on PATH.

    Preflight reads real PATH and the real home directory, so a developer machine
    that happens to have terminal-browser and uv installed would otherwise decide
    these tests. Empty the PATH and point HOME at tmp_path instead; tests that
    want a binary put one there.
    """
    for var in KEY_VARS + ("HERMES_HOME", "WWWDRIVE_HOME", "JEV_DRIVER_HOME", "TERMINAL_BROWSER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))  # a directory that does not exist
    monkeypatch.setattr(pf.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(handler, "LOG_DIR", tmp_path / "logs")
    return tmp_path


# --- shape -----------------------------------------------------------------


def test_shape_is_fixed_and_json_serializable():
    out = pf.preflight()
    assert set(out) == {"decision_key", "terminal_browser", "driver_home", "python_env", "ready", "missing", "fixes"}
    assert set(pf.CHECKS) == {"decision_key", "terminal_browser", "driver_home", "python_env"}
    json.loads(json.dumps(out))  # must survive the MCP content encoding


def test_every_check_is_ok_or_missing_and_agrees_with_ready():
    out = pf.preflight()
    for name in pf.CHECKS:
        assert out[name] in {"ok", "missing"}, name
        assert out[name] == ("ok" if name not in out["missing"] else "missing"), name
    assert out["ready"] is (not out["missing"])
    assert out["missing"] == [name for name in pf.CHECKS if out[name] == "missing"]


def test_fixes_cover_exactly_what_is_missing():
    out = pf.preflight()
    assert set(out["fixes"]) == set(out["missing"])
    assert all(text.strip() for text in out["fixes"].values())
    assert set(pf.FIXES) == set(pf.CHECKS)  # every check has a hint on file


def test_a_fully_provisioned_machine_is_ready(monkeypatch, tmp_path):
    home = tmp_path / "repo"
    (home / "scripts").mkdir(parents=True)
    (home / "scripts" / "drive.py").write_text("# drive\n")
    (home / ".venv").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tb = bindir / "terminal-browser"
    tb.write_text("#!/bin/sh\n")
    tb.chmod(0o755)
    monkeypatch.setenv("WWWDRIVE_HOME", str(home))
    monkeypatch.setenv("PATH", str(bindir))  # no uv here; .venv covers python_env
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-ok")
    out = pf.preflight()
    assert out["ready"] is True
    assert out["missing"] == []
    assert out["fixes"] == {}


# --- secrecy ---------------------------------------------------------------


def test_no_key_value_ever_appears_in_the_output(monkeypatch):
    secret = "sk-or-v1-SUPERSECRET-do-not-leak"
    monkeypatch.setenv("DECISION_GATE_API_KEY", secret)
    blob = json.dumps(pf.preflight())
    assert secret not in blob
    assert "SUPERSECRET" not in blob
    # A key that is present is still only reported as a status.
    assert pf.preflight()["decision_key"] == "ok"


def test_key_in_hermes_env_file_is_detected_without_leaking(monkeypatch, tmp_path):
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    secret = "sk-hermes-SECRET-42"
    (hermes / ".env").write_text(f"# comment\nTYPESAFE_API_KEY={secret}\nOTHER=x\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    out = pf.preflight()
    assert out["decision_key"] == "ok"
    assert secret not in json.dumps(out)


def test_blank_key_counts_as_missing(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "   ")
    assert pf.preflight()["decision_key"] == "missing"


def test_empty_assignment_in_hermes_env_file_counts_as_missing(monkeypatch, tmp_path):
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    (hermes / ".env").write_text('DECISION_GATE_API_KEY=""\n')
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    assert pf.preflight()["decision_key"] == "missing"


def test_no_paths_leaked_into_the_report(monkeypatch, tmp_path):
    """Statuses only: a home directory path is not part of the contract."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-abc")
    out = pf.preflight()
    assert str(tmp_path) not in json.dumps(out)


# --- individual checks -----------------------------------------------------


def test_terminal_browser_found_on_path(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tb = bindir / "terminal-browser"
    tb.write_text("#!/bin/sh\n")
    tb.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert pf.preflight()["terminal_browser"] == "ok"


def test_terminal_browser_missing_without_path_or_override(monkeypatch):
    monkeypatch.setenv("PATH", str(Path("/nonexistent")))
    assert pf.preflight()["terminal_browser"] == "missing"


def test_terminal_browser_override_must_be_executable(monkeypatch, tmp_path):
    fake = tmp_path / "tb"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o644)  # present but not executable
    monkeypatch.setenv("TERMINAL_BROWSER", str(fake))
    assert pf.preflight()["terminal_browser"] == "missing"
    fake.chmod(0o755)
    assert pf.preflight()["terminal_browser"] == "ok"


def test_driver_home_missing_without_the_entrypoint(monkeypatch, tmp_path):
    monkeypatch.setenv("WWWDRIVE_HOME", str(tmp_path / "empty"))
    assert pf.preflight()["driver_home"] == "missing"


def test_driver_home_override_must_hold_drive_py(monkeypatch, tmp_path):
    fake = tmp_path / "elsewhere"
    (fake / "scripts").mkdir(parents=True)
    (fake / "scripts" / "drive.py").write_text("# drive\n")
    monkeypatch.setenv("WWWDRIVE_HOME", str(fake))
    assert pf.preflight()["driver_home"] == "ok"


def test_legacy_driver_home_var_still_resolves(monkeypatch, tmp_path):
    """Pre-1.0 configs set JEV_DRIVER_HOME; the 1.0.0 rename keeps reading it."""
    fake = tmp_path / "elsewhere"
    (fake / "scripts").mkdir(parents=True)
    (fake / "scripts" / "drive.py").write_text("# drive\n")
    monkeypatch.setenv("JEV_DRIVER_HOME", str(fake))
    assert pf.driver_home() == fake
    assert pf.preflight()["driver_home"] == "ok"


def test_wwwdrive_home_wins_over_the_legacy_var(monkeypatch, tmp_path):
    new, old = tmp_path / "new", tmp_path / "old"
    for home in (new, old):
        (home / "scripts").mkdir(parents=True)
        (home / "scripts" / "drive.py").write_text("# drive\n")
    monkeypatch.setenv("WWWDRIVE_HOME", str(new))
    monkeypatch.setenv("JEV_DRIVER_HOME", str(old))
    assert pf.driver_home() == new


def test_python_env_ok_without_uv_when_venv_exists(monkeypatch, tmp_path):
    home = tmp_path / "repo"
    (home / "scripts").mkdir(parents=True)
    (home / "scripts" / "drive.py").write_text("# drive\n")
    (home / ".venv").mkdir()
    monkeypatch.setenv("WWWDRIVE_HOME", str(home))
    monkeypatch.setenv("PATH", str(Path("/nonexistent")))
    assert pf.preflight()["python_env"] == "ok"


def test_python_env_missing_without_uv_or_venv(monkeypatch, tmp_path):
    home = tmp_path / "repo"
    (home / "scripts").mkdir(parents=True)
    (home / "scripts" / "drive.py").write_text("# drive\n")
    monkeypatch.setenv("WWWDRIVE_HOME", str(home))
    monkeypatch.setenv("PATH", str(Path("/nonexistent")))
    assert pf.preflight()["python_env"] == "missing"


def test_missing_everything_is_not_ready(monkeypatch, tmp_path):
    """Bare fixture plus a checkout with no .venv: every check misses."""
    empty_home = tmp_path / "bare"
    (empty_home / "scripts").mkdir(parents=True)
    (empty_home / "scripts" / "drive.py").write_text("# drive\n")
    monkeypatch.setenv("WWWDRIVE_HOME", str(empty_home))
    out = pf.preflight()
    assert out["ready"] is False
    assert out["missing"] == ["decision_key", "terminal_browser", "python_env"]
    assert out["driver_home"] == "ok"  # the entrypoint is there, just nothing to run it with


# --- cli --check -----------------------------------------------------------


def test_check_needs_no_goal():
    args = parse_args(["--check"])
    assert args.check is True
    assert args.goal is None


def test_goal_is_still_required_without_check():
    with pytest.raises(SystemExit) as exc:
        parse_args([])
    assert exc.value.code == 2


def test_goal_still_required_when_blank():
    with pytest.raises(SystemExit) as exc:
        parse_args(["--goal", "   "])
    assert exc.value.code == 2


def test_check_prints_preflight_json_and_exits_zero(capsys, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-check")
    assert main(["--check"]) == 0
    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["decision_key"] == "ok"
    assert set(body) == {"decision_key", "terminal_browser", "driver_home", "python_env", "ready", "missing", "fixes"}
    assert "sk-check" not in out


def test_check_exits_zero_even_when_nothing_is_installed(capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(Path("/nonexistent")))
    monkeypatch.setenv("WWWDRIVE_HOME", str(Path("/nonexistent")))
    assert main(["--check"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ready"] is False
    assert body["missing"]


def test_check_subprocess_is_zero_without_a_goal():
    """End to end through the console entry point: exit 0, one line of JSON."""
    proc = subprocess.run(
        [sys.executable, "-m", "jev_driver.cli", "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    body = json.loads(lines[0])
    assert body["driver_home"] == "ok"
    assert body["ready"] is (not body["missing"])


def test_check_does_not_launch_a_browser(monkeypatch):
    """No spawn, no CDP: --check must not touch the subprocess machinery."""
    import subprocess as sp

    def boom(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("--check spawned a subprocess")

    monkeypatch.setattr(sp, "Popen", boom)
    monkeypatch.setattr(pf.shutil, "which", lambda name: None)
    assert main(["--check"]) == 0


# --- MCP status --------------------------------------------------------


def test_tools_list_includes_status(mcp_mod):
    tools = {t["name"]: t for t in mcp_mod._tool_list()}
    assert "status" in tools
    assert tools["status"]["inputSchema"] == STATUS_PARAMETERS
    assert tools["status"]["inputSchema"]["properties"] == {}
    assert tools["status"]["inputSchema"]["additionalProperties"] is False
    assert tools["status"]["description"]


def test_tools_list_via_dispatch(mcp_mod):
    res = mcp_mod.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in res["result"]["tools"]}
    assert {"drive", "read", "status"} <= names


def test_call_status_works_with_no_browser(monkeypatch, mcp_mod):
    mod = mcp_mod
    monkeypatch.setattr(mod.h, "run_drive", lambda payload: pytest.fail("status launched a drive"))
    monkeypatch.setattr(mod.h, "run_read", lambda payload: pytest.fail("status launched a read"))
    res = mod.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "status", "arguments": {}}}
    )
    assert res["result"]["isError"] is False
    body = json.loads(res["result"]["content"][0]["text"])
    assert body["decision_key"] == "missing"
    assert body["ready"] is False
    assert "driver_key" not in body


def test_call_status_never_leaks_a_key(monkeypatch, mcp_mod):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-mcp-secret")
    res = mcp_mod.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "status", "arguments": {}}}
    )
    text = res["result"]["content"][0]["text"]
    assert "sk-mcp-secret" not in text
    assert json.loads(text)["decision_key"] == "ok"


def test_call_status_takes_no_arguments(mcp_mod):
    """The schema is closed and empty, so arguments are irrelevant either way."""
    mod = mcp_mod
    res = mod.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "status", "arguments": {"goal": "x"}},
        }
    )
    assert res["result"]["isError"] is False
    assert "ready" in json.loads(res["result"]["content"][0]["text"])


def _registered_tool_names(source: str) -> set[str]:
    """Every tool name handed to ctx.register_tool, whatever the calling style.

    AST-based on purpose: a line grep misses `name=` on its own line and misses
    positional calls entirely, so a registration could slip past it.
    """
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "register_tool"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    names.add(keyword.value.value)
        # Positional: ctx.register_tool("status", ...)
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            names.add(node.args[0].value)
    return names


def test_status_is_not_registered_as_a_hermes_native_tool():
    """Statuses are an adapter-level concern; Hermes keeps exactly two native tools."""
    import plugin

    # No schema block to register even if someone tried.
    assert not hasattr(plugin, "STATUS_SCHEMA")

    source = (ROOT / "plugin" / "__init__.py").read_text()
    registered = _registered_tool_names(source)
    # The two real tools prove the extractor sees keyword registrations at all.
    assert {"drive", "read"} <= registered, registered
    assert "status" not in registered, registered


# Each entry is a complete ctx.register_tool(...) call that WOULD register
# status with Hermes. Four spellings, because a line grep only catches one.
_LEAK_STYLES = [
    # name= on a line of its own: the case the old grep-based guard missed.
    '    ctx.register_tool(\n'
    '        name=\n'
    '            "status",\n'
    '        toolset="wwwdrive",\n'
    '        schema=STATUS_SCHEMA,\n'
    '    )\n',
    # The natural multi-line spelling, copied from the two real registrations.
    '    ctx.register_tool(\n'
    '        name="status",\n'
    '        toolset="wwwdrive",\n'
    '        schema=STATUS_SCHEMA,\n'
    '        handler=handle,\n'
    '    )\n',
    # Positional: ctx.register_tool("status", ...)
    '    ctx.register_tool("status", "jev", STATUS_SCHEMA, handle)\n',
    # Every argument keyworded on one line.
    '    ctx.register_tool(name="status", toolset="wwwdrive", schema=STATUS_SCHEMA, handler=handle)\n',
]


@pytest.mark.parametrize("call", _LEAK_STYLES, ids=range(len(_LEAK_STYLES)))
def test_registration_guard_catches_every_call_style(call):
    """Prove the guard bites.

    Each snippet would register status with Hermes if it reached register(),
    and each must be caught, so the real assertion in the test above cannot be
    passing for the wrong reason (e.g. an extractor that finds nothing at all).
    """
    source = (ROOT / "plugin" / "__init__.py").read_text()
    mutated = f"{source}\n\ndef _leak(ctx, handle):\n{call}"
    names = _registered_tool_names(mutated)
    assert "status" in names, f"extractor missed this registration style:\n{call}"
    # And the guard's own conclusion therefore flips to failing.
    assert not ({"drive", "read"} <= names and "status" not in names)


def test_preflight_does_not_import_the_plugin():
    """preflight must answer on its own: the MCP status tool imports it for the
    checks, and a status that needed the Hermes shell could not report a
    Hermes shell that is missing."""
    source = Path(pf.__file__).read_text()
    assert "from plugin" not in source and "import plugin" not in source


def test_mcp_module_does_not_import_the_browser_loop():
    """mcp.py may reach jev_driver only through the stdlib-only preflight leaf:
    nothing that opens a browser, a socket, or a subprocess may be imported."""
    import ast

    source = (ROOT / "scripts" / "mcp.py").read_text()
    assert "from jev_driver.preflight import preflight" in source

    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    driver_modules = {m for m in modules if m.split(".")[0] == "jev_driver"}
    assert driver_modules == {"jev_driver.preflight"}, driver_modules
    for module in driver_modules:
        assert not {"cli", "discover", "cdp", "browser"} & set(module.split(".")), module


def test_preflight_env_is_read_at_call_time(monkeypatch):
    """No import-time caching: the same process must see a key appear and vanish."""
    before = dict(os.environ)
    assert pf.preflight()["decision_key"] == "missing"
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-late")
    assert pf.preflight()["decision_key"] == "ok"
    assert pf.KEY_VARS == ("DECISION_GATE_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert pf.preflight()["decision_key"] == "missing"  # removal is observed too
    # Reading env must not be a write: a check is safe to run mid-session, and
    # must not normalise, default, or reorder anything the caller set.
    assert set(os.environ) == set(before)
    for var, value in before.items():
        assert os.environ[var] == value, var
