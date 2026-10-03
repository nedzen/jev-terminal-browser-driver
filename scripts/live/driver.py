"""MCP stdio transport, and the one-drive-at-a-time rule that makes it safe.

v3 is explicit that the runner is sequential because `scripts/mcp.py` is "serial by
design": while a drive is in flight the server cannot read stdin, so a second
tools/call would queue behind the first and any cancellation would be observed too
late to matter. The mutex itself is held by the caller over megabosss2; what this
module enforces is the local half -- one drive in this process at a time, and a
stall that is noticed from the log rather than only from the socket.

Stall detection is the reason this is not a thin wrapper. v3 defines a stall as no
tick progress in 120s and scores it a MISS; a socket read that only learns about
progress when the call returns cannot see a stall, because a stalled drive never
returns. So progress is watched in the run log while the call is outstanding.

Stdlib only. No browser is launched here: the server subprocess does that, and
this module only speaks JSON-RPC to it.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "scripts" / "mcp.py"

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")
PROTOCOL = SUPPORTED_PROTOCOLS[0]

# One drive at a time, per process. A class-level guard rather than an instance
# one, because the rule is about the pane, not about a client object.
_ACTIVE = {"drive": False}


class DriverError(RuntimeError):
    """The transport failed in a way the run must record rather than retry."""


class Stalled(RuntimeError):
    """No tick progress inside the stall window: v3.1 scores this STALL."""


class CallTimeout(RuntimeError):
    """The call outlived timeout_s without returning.

    Distinct from DriverError because v3.1 scores the two differently: a timeout
    is the STALL class, while a transport failure is CRASH. Collapsing them would
    report a slow site as a broken harness.
    """


def _claim(tool: str) -> None:
    if _ACTIVE["drive"]:
        raise DriverError("another drive is already in flight on this pane")
    _ACTIVE["drive"] = True


def _release() -> None:
    _ACTIVE["drive"] = False


class McpStdio:
    """A newline-delimited JSON-RPC client for `scripts/mcp.py`.

    Deliberately not a context manager around the whole suite: the server is
    long-lived on purpose (one process serves many runs) and re-initialising per
    test would cost a handshake per run for no isolation benefit.
    """

    def __init__(self, command=None, *, cwd=None):
        self.command = command or ["uv", "run", "--directory", str(REPO_ROOT), "python", str(SERVER)]
        self.cwd = cwd or str(REPO_ROOT)
        self.proc = None
        self._id = 0
        self.server_info = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "McpStdio":
        try:
            self.proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
            )
        except OSError as exc:
            raise DriverError(f"could not start the MCP server: {exc}") from exc
        init = self.rpc("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "live-harness", "version": "0"},
        })
        result = (init or {}).get("result") or {}
        self.server_info = result.get("serverInfo") or {}
        negotiated = result.get("protocolVersion")
        if negotiated not in SUPPORTED_PROTOCOLS:
            raise DriverError(f"server negotiated an unsupported protocol: {negotiated!r}")
        self.rpc("notifications/initialized", {}, notify=True)
        return self

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.close()
        return False

    # -- transport ---------------------------------------------------------

    def rpc(self, method: str, params: dict | None = None, *, notify: bool = False):
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise DriverError("the MCP server is not running")
        self._id += 1
        message = {"jsonrpc": "2.0", "method": method}
        if not notify:
            message["id"] = self._id
        if params is not None:
            message["params"] = params
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise DriverError(f"the MCP server closed its input: {exc}") from exc
        if notify:
            return None
        line = self.proc.stdout.readline()
        if not line:
            raise DriverError("the MCP server closed its output")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise DriverError(f"unparseable response: {line[:200]!r}") from exc

    def tools(self) -> list[str]:
        result = (self.rpc("tools/list") or {}).get("result") or {}
        return [tool.get("name") for tool in result.get("tools") or []]

    # -- calls -------------------------------------------------------------

    def call(self, name: str, arguments: dict, *, log_dir=None, stall_s=None, timeout_s=None):
        """One tools/call, with the pane guard and stall watching.

        `stall_s` and `log_dir` together enable stall detection. Without them the
        call is a plain blocking read, which is what the self-tests use and what a
        caller gets if it explicitly declines the check.
        """
        if name == "drive":
            _claim(name)
        try:
            started = time.perf_counter()
            if stall_s and log_dir is not None:
                return self._call_with_stall(name, arguments, log_dir, stall_s, started, timeout_s)
            response = self.rpc("tools/call", {"name": name, "arguments": arguments})
            if timeout_s and (time.perf_counter() - started) > timeout_s:
                raise CallTimeout(f"{name} exceeded timeout_s={timeout_s}")
            return response
        finally:
            if name == "drive":
                _release()

    def _call_with_stall(self, name, arguments, log_dir, stall_s, started, timeout_s=None):
        """Issue the call on a thread, watching the run log for tick progress.

        The read blocks in a worker so progress and elapsed time can both be
        observed while the call is outstanding -- which is the only way either can
        be, because a stalled or hung drive never returns. A daemon thread that
        outlives either is deliberate: the server will not answer until the drive
        finishes, and killing this process would orphan that subprocess (mcp.py
        says so in its own docstring).
        """
        import threading

        box: dict = {}

        def worker():
            try:
                box["response"] = self.rpc("tools/call", {"name": name, "arguments": arguments})
            except Exception as exc:  # surfaced to the caller below
                box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        from scripts.live.isolation import LOG_DIR  # local: keeps the import graph acyclic

        path = Path(log_dir or LOG_DIR) / "drive.jsonl"
        size = path.stat().st_size if path.is_file() else 0
        last_progress = time.monotonic()
        while thread.is_alive():
            thread.join(timeout=0.5)
            if not thread.is_alive():
                break
            if path.is_file():
                try:
                    grown = path.stat().st_size
                except OSError:
                    grown = size
                if grown != size:
                    size = grown
                    last_progress = time.monotonic()
            if time.monotonic() - last_progress > stall_s:
                raise Stalled(f"no tick progress within {stall_s}s")
            if timeout_s and (time.perf_counter() - started) > timeout_s:
                raise CallTimeout(f"{name} exceeded timeout_s={timeout_s}")
        if "error" in box:
            raise DriverError(str(box["error"]))
        if "response" not in box:
            raise DriverError(f"{name} returned no response")
        return box["response"]

    @staticmethod
    def payload(response) -> dict:
        """The JSON object a tools/call carried, i.e. what the caller ingested.

        This is the object measurement contract (a) measures: not the envelope,
        not the server's stdout, the result the caller actually received.
        """
        result = (response or {}).get("result") or {}
        content = result.get("content") or []
        if not content:
            return {}
        try:
            return json.loads(content[0].get("text") or "{}")
        except (ValueError, AttributeError):
            return {}
