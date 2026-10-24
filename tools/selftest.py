"""One-command end-to-end self test (no phone, no game needed).

Starts pc/server.py in a subprocess, waits for /healthz, then runs the headless
phone simulator (tools/test_client.py) against it and reports PASS/FAIL.

    python tools/selftest.py
    python tools/selftest.py --real-window      # use the actual game window
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def free_port(start=8770) -> int:
    for port in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port")


def wait_health(port: int, timeout: float = 25.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                return json.loads(response.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.3)
    raise RuntimeError(f"server never became healthy: {last}")


def get_status(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-window", action="store_true", help="use the game window instead of a fixed region")
    parser.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()

    port = free_port()
    cmd = [sys.executable, os.path.join(ROOT, "pc", "server.py"), "--port", str(port), "--touch", "log"]
    if not args.real_window:
        cmd += ["--region", "0,0,640,640"]
    print("[i] starting:", " ".join(cmd))
    server = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
    ok = False
    try:
        wait_health(port)
        print(f"[i] server healthy on :{port}")
        client = subprocess.run(
            [sys.executable, os.path.join(HERE, "test_client.py"), "--url", f"127.0.0.1:{port}", "--seconds", str(args.seconds)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        print(client.stdout.strip())
        if client.stderr.strip():
            print("[client stderr]", client.stderr.strip()[-2000:])
        ok = "VERDICT           : PASS" in client.stdout
        status = get_status(port)
        print("\n[i] final /api/status:")
        print(json.dumps(status, ensure_ascii=False, indent=1)[:1500])
    finally:
        server.terminate()
        try:
            out, _ = server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            out, _ = server.communicate()
        print("\n[i] --- server log ---")
        print("\n".join((out or "").splitlines()[-25:]))
    print("\nSELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
