#!/usr/bin/env python3
"""Run a command, stream its output, and publish any failure where it can be read.

A failing GitHub Actions step on its own only says "Process completed with exit
code 1". This wrapper streams the child's output as it happens, keeps the tail,
and on failure

  * emits an ``::error`` workflow command, so the reason appears as an
    annotation on the check run itself (visible from the API and from a phone),
  * appends the tail to ``$GITHUB_STEP_SUMMARY``.

The wrapper is deliberately stdlib-only and cross-platform: on Windows the
default shell is PowerShell, so anything doing real work is better expressed as
a Python command than as shell syntax.

Usage
    python scripts/ci_run.py --title "Executable plug-in specification" \\
        -- python -m tests.lightroom.lua_harness
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections import deque

MAX_ANNOTATION = 60000  # GitHub rejects annotations above 64 KiB


def _escape(value: str) -> str:
    """Escape a workflow-command message (GitHub's %-encoding)."""
    return (value.replace("%", "%25")
                 .replace("\r", "")
                 .replace("\n", "%0A"))


def publish(title: str, tail: str, returncode: int) -> None:
    message = f"{title} failed (exit {returncode}); last output follows:\n{tail}"
    message = message[-MAX_ANNOTATION:]
    print(f"::error title={title}::{_escape(message)}", flush=True)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(f"## ❌ {title} failed (exit {returncode})\n\n")
            handle.write("~~~\n" + tail + "\n~~~\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--title", default="Command", help="annotation title")
    parser.add_argument("--tail", type=int, default=160, help="lines kept for the report")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="command to run (prefix it with --)")
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command given; use: ci_run.py --title X -- <command>")

    buffer: deque[str] = deque(maxlen=max(args.tail, 10))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        buffer.append(line.rstrip("\n"))
        print(line, end="", flush=True)
    returncode = process.wait()

    if returncode != 0:
        publish(args.title, "\n".join(buffer), returncode)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
