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
        env = dict(os.environ, KESTREL_HOME=str(self.home))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "kestrel.api.app:create_app",
             "--factory", "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode() if self.proc.stdout else ""
                raise RuntimeError(f"engine died during startup:\n{out}")
            try:
                with urllib.request.urlopen(self.base_url + "/api/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("engine did not become healthy in time")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if PLUGIN_DIR.exists():
            shutil.rmtree(self.home, ignore_errors=True)


# ------------------------------------------------------------------- lua runner
def run_spec(base_url: str, keep_home: bool = False) -> dict:
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

    try:
        result = run_spec(base_url)
    finally:
        if engine:
            engine.stop()

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for case in result.get("cases", []):
            mark = "PASS" if case.get("ok") else "FAIL"
            print(f"{mark}  {case.get('name')}")
            if not case.get("ok"):
                print(f"      {case.get('error')}")
        print(f"\n{result.get('passed', 0)} passed, {result.get('failed', 0)} failed")
    return 0 if result.get("failed", 1) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
