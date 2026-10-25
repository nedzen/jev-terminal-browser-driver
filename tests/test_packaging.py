"""Packaging contract: the installed distribution can serve MCP without a checkout.

These pin the release shape that 1.1.0 lacked: `scripts/` never ships, so any
adapter that re-executes a repo script only works from a git clone. The entry
points and the module resolution below are what make `pipx install wwwdrive`
usable by an arbitrary MCP client.
"""

import importlib.metadata
import sys
import tomllib
from pathlib import Path

import pytest

from plugin import handler

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_the_server_and_both_clis_have_console_scripts():
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["wwwdrive"] == "jev_driver.cli:main"
    assert scripts["wwwdrive-mcp"] == "jev_driver.mcp:main"
    assert scripts["wwwdrive-read"] == "jev_driver.read_cli:main"


def test_agent_state_and_lab_material_are_excluded_from_the_build():
    excluded = "\n".join(_pyproject()["tool"]["hatch"]["build"]["exclude"])
    for path in ("plugin/.crush", "plugin/.cursor", "scripts/live", "tests", "docs"):
        assert path in excluded, path


def test_the_wire_version_comes_from_the_distribution():
    from jev_driver import mcp

    assert mcp.SERVER_VERSION == importlib.metadata.version("wwwdrive")


def test_driver_runs_as_a_module_when_the_package_is_importable():
    """The wheel path: this interpreter owns the driver, so no repo script is named."""
    assert handler._module_available("jev_driver.cli")
    argv = handler.driver_argv()
    assert argv == [sys.executable, "-m", "jev_driver.cli"]


def test_read_runs_as_a_module_when_the_package_is_importable():
    assert handler._module_available("jev_driver.read_cli")
    assert handler.read_argv() == [sys.executable, "-m", "jev_driver.read_cli"]


def test_checkout_shims_still_forward(monkeypatch):
    """Pre-1.2 client configs point at scripts/mcp.py; that path must keep serving."""
    import scripts.mcp as shim

    assert callable(shim.main)


def test_no_hard_coded_version_outside_the_manifests():
    """pyproject is the source; plugin.yaml is read by the Hermes catalog.

    Everything else (the MCP serverInfo, uv.lock) is derived, so a release bump
    cannot leave one screen reporting an older product than another.
    """
    offenders = []
    # Walk product trees only: a recursive scan of the checkout also reads uv's
    # cache and any vendored dependency, which are not our release surface.
    for base in ("jev_driver", "plugin", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            # scripts/live names past releases as test targets; it is lab, not product.
            if path.relative_to(ROOT).parts[:2] == ("scripts", "live"):
                continue
            if "1.1.0" in path.read_text(errors="ignore"):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders


@pytest.mark.parametrize("module", ["jev_driver.mcp", "jev_driver.read_cli"])
def test_packaged_entry_modules_import_cleanly(module):
    __import__(module)
