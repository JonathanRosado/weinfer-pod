#!/usr/bin/env python3
"""Wait for the loopback vLLM health endpoint while proving its PID is alive."""

from __future__ import annotations

import argparse
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.pid < 1 or not 1 <= args.timeout_seconds <= 3600:
        parser.error("pid must be positive and timeout must be within 1..3600 seconds")

    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        if not pid_alive(args.pid):
            raise SystemExit("vLLM exited before engine readiness")
        try:
            with urlopen("http://127.0.0.1:8000/health", timeout=2) as response:
                if response.status == 200:
                    print("vLLM loopback health verified")
                    return 0
        except (HTTPError, URLError, TimeoutError, OSError):
            pass
        time.sleep(2)
    raise SystemExit("vLLM engine readiness timeout")


if __name__ == "__main__":
    raise SystemExit(main())
