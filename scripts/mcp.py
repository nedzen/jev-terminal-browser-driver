#!/usr/bin/env python3
"""Checkout shim: the MCP server lives in `jev_driver.mcp` (installed as `wwwdrive-mcp`).

Kept so client configs registered before 1.2 keep working unchanged:
    {"command": "uv", "args": ["run", "--directory", "<repo>", "python", "scripts/mcp.py"]}
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jev_driver.mcp import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
