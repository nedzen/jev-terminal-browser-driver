"""Jev chooses an observed action. Code owns execution."""

from importlib.metadata import PackageNotFoundError, version

from .agent import Agent
from .browser import Browser

try:  # the wheel has metadata; a raw checkout may not
    __version__ = version("wwwdrive")
except PackageNotFoundError:  # pragma: no cover - development checkout
    __version__ = "0.0.0+unknown"

__all__ = ["Agent", "Browser", "__version__"]
