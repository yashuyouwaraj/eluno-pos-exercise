#!/usr/bin/env python3
"""Start the mock, POS API, and interactive cashier in one terminal."""

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
MOCK_PORT = 8787
APP_PORT = 8790


def assert_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(
                "port {} is already accepting connections; refusing to start a duplicate process".format(port)
            )


def wait_for_health(url: str, process: subprocess.Popen, label: str) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("{} exited during startup with code {}".format(label, process.returncode))
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    response.read()
                    return
        except URLError:
            time.sleep(0.1)
    raise RuntimeError("{} did not become healthy at {} within 8 seconds".format(label, url))


def stop(processes: List[subprocess.Popen]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for process in reversed(processes):
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def start_process(command: List[str], env=None) -> subprocess.Popen:
    """Give child services their own Windows process groups for reliable Ctrl+C cleanup."""
    options = {"cwd": str(ROOT), "env": env}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(command, **options)


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the complete local cashier environment")
    parser.add_argument("--no-cli", action="store_true", help="start only mock and API (developer use)")
    parser.add_argument("--mock-port", type=int, default=MOCK_PORT, help=argparse.SUPPRESS)
    parser.add_argument("--app-port", type=int, default=APP_PORT, help=argparse.SUPPRESS)
    parser.add_argument(
        "--mock-state-db",
        default=str(ROOT / "eluno_pos_mock_state.sqlite3"),
        help="SQLite file for persistent local Shopify/SMT/terminal mock data",
    )
    args = parser.parse_args()
    processes = []
    try:
        assert_port_available(args.mock_port)
        assert_port_available(args.app_port)
        mock = start_process(
            [sys.executable, "-u", str(ROOT / "starter-kit" / "mock-apis" / "mock_server.py"), "--no-flake", "--port", str(args.mock_port), "--state-db", args.mock_state_db],
        )
        processes.append(mock)
        wait_for_health("http://127.0.0.1:{}/health".format(args.mock_port), mock, "mock API")

        app_env = os.environ.copy()
        app_env.update({
            "ELUNO_SHOPIFY_BASE_URL": "http://127.0.0.1:{}/admin/api/2026-10".format(args.mock_port),
            "ELUNO_SMT_BASE_URL": "http://127.0.0.1:{}/smt".format(args.mock_port),
            "ELUNO_TERMINAL_BASE_URL": "http://127.0.0.1:{}/terminal".format(args.mock_port),
            "ELUNO_LOCAL_MOCK_MODE": "1",
            "ELUNO_LOCAL_MOCK_PORT": str(args.mock_port),
        })
        app = start_process(
            [sys.executable, "-u", "-m", "eluno_pos", "--port", str(args.app_port)], env=app_env,
        )
        processes.append(app)
        wait_for_health("http://127.0.0.1:{}/health".format(args.app_port), app, "Eluno POS application")
        print("Local environment ready: mock=http://127.0.0.1:{} app=http://127.0.0.1:{}".format(args.mock_port, args.app_port), flush=True)
        if args.no_cli:
            print("API-only mode. Press Ctrl+C to stop both services.")
            while True:
                time.sleep(1)
        print("Starting cashier CLI. Exit it (option 0) to stop all local services.", flush=True)
        return subprocess.call([sys.executable, "-m", "eluno_pos.cli", "--base-url", "http://127.0.0.1:{}".format(args.app_port), "--mock-url", "http://127.0.0.1:{}/admin/api/2026-10".format(args.mock_port)], cwd=str(ROOT))
    except KeyboardInterrupt:
        print("\nStopping local environment...")
        return 0
    except RuntimeError as exc:
        print("Startup failed: {}".format(exc), file=sys.stderr)
        return 1
    finally:
        stop(processes)


if __name__ == "__main__":
    sys.exit(main())
