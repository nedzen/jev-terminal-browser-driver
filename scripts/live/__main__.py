"""Runnable entry point for the live S-batch.

Usage (needs a real terminal-browser + TYPESAFE_API_KEY on the host):

    uv run python -m scripts.live                     # all manifests/*.json
    uv run python -m scripts.live S2a S7c             # by id
    uv run python -m scripts.live path/to/suite.json  # explicit file

Each file under manifests/ may be either a full suite ``{"tests": [...]}`` or a
single test object (the shape Marius kept in /tmp). Single-test files are wrapped
automatically. Stdlib only; the runner talks to ``scripts/mcp.py`` over stdio.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.live.runner import SuiteAbort, run_suite
from scripts.live.spec import ManifestError, validate_manifest

MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"


def _load_file(path: Path) -> dict:
    """One JSON file → a suite dict the runner accepts.

    A bare test object (has ``id``, no ``tests``) is wrapped so the manifests
    Marius kept as one-file-per-S-case stay runnable without rewriting them.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: root must be an object")
    if "tests" in data:
        data.setdefault("suite", path.stem)
        return data
    if "id" in data:
        return {"suite": str(data["id"]), "tests": [data]}
    raise ManifestError(f"{path}: need a 'tests' list or a single test with 'id'")


def _resolve(args: list[str]) -> list[Path]:
    """Ids, paths, or (default) every *.json under manifests/.

    When both ``S4a.json`` and ``S4a-chain.json`` exist, the default suite prefers
    the chain: a single no-url drive after quarantine is a setup error (no_page),
    not the continuity case the id names.
    """
    if not args:
        paths = sorted(MANIFEST_DIR.glob("*.json"))
        if not paths:
            raise ManifestError(f"no manifests in {MANIFEST_DIR}")
        chained = {p.name[: -len("-chain.json")] for p in paths if p.name.endswith("-chain.json")}
        return [p for p in paths if p.stem not in chained]
    out: list[Path] = []
    for arg in args:
        path = Path(arg)
        if path.is_file():
            out.append(path.resolve())
            continue
        by_id = MANIFEST_DIR / f"{arg}.json"
        if by_id.is_file():
            out.append(by_id)
            continue
        raise ManifestError(f"not a manifest file or id under {MANIFEST_DIR}: {arg!r}")
    return out


def _merge(paths: list[Path]) -> dict:
    """Many files → one suite; duplicate ids are refused by validate_manifest."""
    tests: list[dict] = []
    for path in paths:
        suite = _load_file(path)
        tests.extend(suite["tests"])
    return {"suite": "live", "tests": tests}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the wwwdrive live S-batch.")
    parser.add_argument(
        "targets",
        nargs="*",
        help="Manifest ids (S2a), JSON paths, or nothing for all of manifests/",
    )
    parser.add_argument(
        "--dry-validate",
        action="store_true",
        help="Validate and print the suite; do not start a browser or MCP server.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Driver state dir override (default: product cache).",
    )
    parser.add_argument(
        "--slice-dir",
        type=Path,
        default=Path("/tmp/wwwdrive-runs"),
        help="Where to write per-run JSONL slices.",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Learnings ledger path (default: docs/live-learnings.md).",
    )
    ns = parser.parse_args(argv)

    try:
        paths = _resolve(ns.targets)
        manifest = validate_manifest(_merge(paths))
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 2

    if ns.dry_validate:
        ids = [t["id"] for t in manifest["tests"]]
        print(json.dumps({"suite": manifest["suite"], "tests": ids, "files": [str(p) for p in paths]}, indent=2))
        return 0

    # Lazy import of the product state dir so --dry-validate never needs the driver.
    from scripts.live.runner import LEDGER, driver_state_dir

    log_dir = ns.log_dir or driver_state_dir()
    try:
        board = run_suite(
            manifest,
            log_dir=log_dir,
            slice_dir=ns.slice_dir,
            suite=manifest["suite"],
            ledger=ns.ledger if ns.ledger is not None else LEDGER,
        )
    except SuiteAbort as exc:
        print(f"suite abort: {exc}", file=sys.stderr)
        return 3

    # Compact scoreboard for a human paste; full records stay in the ledger/slices.
    summary = {k: v for k, v in board.items() if k != "records"}
    print(json.dumps(summary, indent=2, default=str))
    if board.get("sev1_count"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
