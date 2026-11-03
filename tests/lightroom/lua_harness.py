"""Run the Kestrel Lightroom plug-in outside Lightroom.

The plug-in is plain Lua 5.1 using only documented SDK namespaces. This harness
executes the *real* plug-in files (Info.lua loader, Core, Http, Json, Apply,
Workflow, Batch, Menus, Dialogs) against:

  * a real Kestrel engine on a throw-away data directory (HTTP is genuinely
    exercised through LrHttp's contract — body + headers table with .status);
  * mock Lr* namespaces that record calls, so we can assert what the plug-in
    asked Lightroom to do (develop settings written, snapshots kept, TIFFs
    added to the catalog).

Usage:
    python -m tests.lightroom.lua_harness            # start engine, run all
    python -m tests.lightroom.lua_harness --port 8765 --external
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Tuple

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "lightroom" / "Kestrel.lrplugin"
SPEC_PATH = Path(__file__).resolve().parent / "plugin_spec.lua"
SAMPLE = ROOT / "assets" / "samples" / "football_night.jpg"


# --------------------------------------------------------------------------- http
def _http(method: str, url: str, body: str | None, headers: Any,
          timeout: float) -> Tuple[int, str, dict]:
    """The contract LrHttp.get/post implement, so the plug-in's Http.lua is real."""
    data = body.encode("utf-8") if isinstance(body, str) and body != "" else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout or 30) as response:
            return response.status, response.read().decode("utf-8", "replace"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers or {})
    except Exception as exc:  # network failure → LrHttp returns nil + info
        raise ConnectionError(str(exc))


# ----------------------------------------------------------------- engine setup
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class EngineProcess:
    def __init__(self) -> None:
        self.port = free_port()
        self.home = Path(tempfile.mkdtemp(prefix="kestrel-plugin-test-"))
        self.proc: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        # The engine writes to a file rather than a pipe: a pipe that nobody
        # drains blocks the child once the OS buffer fills (16 KiB on macOS,
        # 64 KiB on Linux), which would look like a mysterious hang in CI.
        self.log_path = self.home / "engine.log"
        self._log = open(self.log_path, "wb")
        env = dict(os.environ, KESTREL_HOME=str(self.home))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "kestrel.api.app:create_app",
             "--factory", "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"],
            cwd=str(ROOT), env=env, stdout=self._log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"engine died during startup:\n{self.tail()}")
            try:
                with urllib.request.urlopen(self.base_url + "/api/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError(
            f"engine did not become healthy in {120} s (exit {self.proc.poll()}):\n{self.tail()}")

    def tail(self, lines: int = 60) -> str:
        """Last lines of the engine log — included in every startup failure."""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return "(engine log unavailable)"
        return "\n".join(text.splitlines()[-lines:])

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "_log", None) is not None:
            self._log.close()
            self._log = None
        shutil.rmtree(self.home, ignore_errors=True)


# ------------------------------------------------------------------- lua runner
def run_spec(base_url: str, on_case=None) -> dict:
    """Execute plugin_spec.lua against the plug-in files, case by case.

    ``on_case(name, ok, error)`` is called after every case, so a long run
    reports progress instead of going silent until the end.
    """
    from lupa import LuaRuntime  # imported lazily for a clear error message

    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()

    workdir = Path(tempfile.mkdtemp(prefix="kestrel-lua-"))
    second = workdir / "second_frame.jpg"
    shutil.copyfile(SAMPLE, second)

    g.TEST_BASE_URL = base_url
    g.TEST_PLUGIN_DIR = str(PLUGIN_DIR)
    g.TEST_SAMPLE = str(SAMPLE)
    g.TEST_WORKDIR = str(workdir)
    g.TEST_SAMPLE2 = str(second)
    g.py_http = _http
    g.py_sleep = time.sleep
    g.py_progress = on_case or (lambda name, ok, error: None)

    with open(SPEC_PATH, "r", encoding="utf-8") as fh:
        source = fh.read()
    chunk = lua.compile(source, "plugin_spec")
    result = chunk()
    return _lua_to_python(result)


def _lua_to_python(value):
    """Convert lupa tables into plain Python structures (arrays stay lists)."""
    if isinstance(value, dict):
        return {str(k): _lua_to_python(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_lua_to_python(v) for v in value]
    items = getattr(value, "items", None)
    if callable(items):
        pairs = [(k, v) for k, v in items()]
        if pairs and all(isinstance(k, int) for k, _ in pairs):
            return [_lua_to_python(v) for k, v in sorted(pairs)]
        if not pairs:
            return {}
        return {str(k): _lua_to_python(v) for k, v in pairs}
    return value


# ---------------------------------------------------------------------- reporting
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="use an existing engine")
    parser.add_argument("--external", action="store_true",
                        help="do not start/stop an engine (requires --port)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    engine = None
    if args.external:
        if not args.port:
            parser.error("--external requires --port")
        base_url = f"http://127.0.0.1:{args.port}"
    else:
        engine = EngineProcess()
        if args.port:
            engine.port = args.port
        engine.start()
        base_url = engine.base_url

    def on_case(name: str, ok: bool, error: str) -> None:
        if args.json:
            return
        mark = "PASS" if ok else "FAIL"
        print(f"{mark}  {name}" + ("" if ok else f"\n      {error}"), flush=True)

    try:
        result = run_spec(base_url, on_case=on_case)
    finally:
        if engine:
            engine.stop()

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        failing = [case for case in result.get("cases", []) if not case.get("ok")]
        for case in failing:
            print(f"FAIL  {case.get('name')}\n      {case.get('error')}")
        print(f"\n{result.get('passed', 0)} passed, {result.get('failed', 0)} failed")
    return 0 if result.get("failed", 1) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
