"""Measure local TUI milestones through a PTY without retaining terminal output."""

from __future__ import annotations

import argparse
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from statistics import median

WELCOME_MARKER = b"my-code"
INPUT_MARKER = "›".encode()
READY_MARKER = b"Ready"


def _run_once(workspace: Path, config_dir: Path, timeout: float) -> dict[str, float]:
    master, slave = pty.openpty()
    command = [
        sys.executable,
        "-c",
        "from my_code.bootstrap import main; main()",
        "--cwd",
        str(workspace),
    ]
    environ = dict(os.environ)
    environ["MY_CODE_CONFIG_DIR"] = str(config_dir)
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environ,
        close_fds=True,
    )
    os.close(slave)
    captured = bytearray()
    milestones: dict[str, float] = {"process_entry_ms": 0.0}
    try:
        while time.perf_counter() - started < timeout:
            readable, _, _ = select.select([master], [], [], 0.05)
            if readable:
                try:
                    captured.extend(os.read(master, 65_536))
                except OSError:
                    break
            elapsed_ms = (time.perf_counter() - started) * 1000
            if "welcome_ms" not in milestones and WELCOME_MARKER in captured:
                milestones["welcome_ms"] = elapsed_ms
            if "input_ready_ms" not in milestones and INPUT_MARKER in captured:
                milestones["input_ready_ms"] = elapsed_ms
            if "capabilities_ready_ms" not in milestones and READY_MARKER in captured:
                milestones["capabilities_ready_ms"] = elapsed_ms
                os.write(master, b"\x04")
            if process.poll() is not None:
                break
            if "capabilities_ready_ms" in milestones:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                break
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        os.close(master)
    required = {"welcome_ms", "input_ready_ms", "capabilities_ready_ms"}
    if missing := required - milestones.keys():
        raise RuntimeError(
            f"Startup markers not observed: {', '.join(sorted(missing))}"
        )
    return milestones


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.runs < 2:
        parser.error("--runs must be at least 2 so the first sample can be discarded")

    with tempfile.TemporaryDirectory(prefix="my-code-startup-") as directory:
        config_dir = Path(directory) / "config"
        samples = [
            _run_once(args.cwd.resolve(), config_dir, args.timeout)
            for _ in range(args.runs)
        ]
    measured = samples[1:]
    result = {
        "samples": samples,
        "median_welcome_ms": median(item["welcome_ms"] for item in measured),
        "median_input_ready_ms": median(item["input_ready_ms"] for item in measured),
        "median_capabilities_ready_ms": median(
            item["capabilities_ready_ms"] for item in measured
        ),
    }
    print(json.dumps(result, indent=2))
    return 0 if result["median_welcome_ms"] <= 1500 else 1


if __name__ == "__main__":
    raise SystemExit(main())
