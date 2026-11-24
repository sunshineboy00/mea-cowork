"""End-to-end harness for the browser screencast stream.

These tests drive the **real stack** — a real ``octop run`` backend, a real
Chrome over CDP, and the real WebSocket endpoints — so they are opt-in:

    OCTOP_E2E_BROWSER=1 uv run pytest tests/e2e/browser_stream -q

Every test is also tagged ``slow`` so ``make test-fast`` skips it. Without
``OCTOP_E2E_BROWSER=1`` the whole module is skipped, which keeps the default
``pytest`` run hermetic.

Environment knobs:

* ``OCTOP_E2E_BROWSER=1``    enable the suite (required)
* ``OCTOP_E2E_KEEP_HOME=1``  keep the throwaway OCTOP_HOME for inspection
* ``BROWSER_USE_CHROME_BIN`` pin a Chrome binary instead of auto-discovery
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

_PAGES_DIR = Path(__file__).resolve().parent / "pages"
_E2E_ENABLED = os.environ.get("OCTOP_E2E_BROWSER") == "1"
_BOOT_TIMEOUT_S = 90.0

ADMIN_USER = "e2eadmin"
ADMIN_PASSWORD = "E2ePassw0rd!x"

# The repository's session fixtures relocate ``$HOME`` to an isolated directory,
# which hides Playwright's browser cache from ``find_chrome()``. Resolve the
# binary at import time — before any fixture runs — and remember the real home
# so spawned processes can still find it.
_REAL_HOME = Path(os.path.expanduser("~")).resolve()


def _probe_chrome() -> str | None:
    explicit = os.environ.get("BROWSER_USE_CHROME_BIN")
    if explicit:
        return explicit
    try:
        from octop_browser.cdp.launcher import find_chrome

        return find_chrome()
    except Exception:  # noqa: BLE001 — discovery is best-effort
        return None


_CHROME_BIN = _probe_chrome()


def resolve_chrome() -> str | None:
    """Path to a usable Chrome, or None (the caller skips the test)."""
    return os.environ.get("BROWSER_USE_CHROME_BIN") or _CHROME_BIN


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip this suite unless explicitly enabled.

    Hooks declared in a conftest apply to the *whole* session, so the skip is
    scoped by path — otherwise every test in the repository would be skipped.
    """
    if _E2E_ENABLED:
        return
    here = Path(__file__).resolve().parent
    skip = pytest.mark.skip(reason="browser stream E2E: set OCTOP_E2E_BROWSER=1 to run")
    for item in items:
        path = Path(str(getattr(item, "path", ""))).resolve()
        if here == path.parent or here in path.parents:
            item.add_marker(skip)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_http(url: str, timeout: float = _BOOT_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                if resp.status < 500:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"{url} did not become ready: {last}")


def post_json(url: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(  # noqa: S310
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read() or b"{}")


def get_json(url: str, token: str | None = None) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")  # noqa: S310
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read() or b"{}")


@dataclass(frozen=True)
class Backend:
    """A live ``octop run`` instance on a throwaway OCTOP_HOME."""

    base_url: str
    home: Path
    token: str


def _bootstrap(home: Path, base_url: str) -> str:
    """Walk the first-run wizard, then log in and return an access token."""
    status = get_json(f"{base_url}/api/setup/status")
    if status.get("setup_required"):
        password_file = home / "octop-login.txt"
        password = password_file.read_text(encoding="utf-8").strip()
        wizard = post_json(
            f"{base_url}/api/setup/verify-password", {"password": password}
        )["wizard_token"]
        post_json(
            f"{base_url}/api/setup/database",
            {"driver": "sqlite", "sqlite_path": str(home / "e2e.db")},
        )
        post_json(
            f"{base_url}/api/setup/initial-admin",
            {
                "username": ADMIN_USER,
                "password": ADMIN_PASSWORD,
                "display_name": "E2E Admin",
            },
            token=wizard,
        )
    login = post_json(
        f"{base_url}/api/auth/login",
        {"username": ADMIN_USER, "password": ADMIN_PASSWORD},
    )
    return str(login["access_token"])


@pytest.fixture(scope="session")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Backend]:
    """Boot a real backend once per session."""
    home = tmp_path_factory.mktemp("octop-home")
    port = free_port()
    env = {
        **os.environ,
        "HOME": str(_REAL_HOME),
        "OCTOP_HOME": str(home),
        # macOS reports no sandbox requirement, but restricted hosts (CI
        # containers, sandboxes) need it or Chrome aborts before binding CDP.
        "BROWSER_USE_NO_SANDBOX": "1",
    }
    if _CHROME_BIN:
        env["BROWSER_USE_CHROME_BIN"] = _CHROME_BIN
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from octop.cli.main import cli; cli()",
            "run",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_http(f"{base_url}/api/setup/status")
        token = _bootstrap(home, base_url)
        yield Backend(base_url=base_url, home=home, token=token)
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=15)
        if os.environ.get("OCTOP_E2E_KEEP_HOME") != "1":
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(scope="session")
def pages_url() -> Iterator[str]:
    """Serve the fixture pages over HTTP.

    Screencast targets must be real URLs: the server's URL normalizer rewrites
    ``data:`` targets into ``https://data:...`` and navigation fails.
    """

    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(_PAGES_DIR), **kwargs)

        def log_message(self, *args: Any) -> None:  # silence the access log
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@dataclass
class Chrome:
    """A headless Chrome for Testing with a CDP endpoint."""

    port: int
    profile: Path
    proc: subprocess.Popen[bytes]

    def ws_url(self, timeout: float = 30.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                targets = json.loads(
                    urllib.request.urlopen(  # noqa: S310
                        f"http://127.0.0.1:{self.port}/json", timeout=3
                    ).read()
                )
                pages = [t for t in targets if t.get("type") == "page"]
                if pages:
                    return str(pages[0]["webSocketDebuggerUrl"])
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.25)
        raise RuntimeError("chrome devtools endpoint did not come up")


def launch_chrome(profile: Path, *, window: str = "1440,900") -> Chrome:
    binary = resolve_chrome()
    if not binary:
        pytest.skip("no Chrome/Chromium found for the E2E browser tests")
    port = free_port()
    proc = subprocess.Popen(  # noqa: S603
        [
            binary,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={window}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    chrome = Chrome(port=port, profile=profile, proc=proc)
    chrome.ws_url()  # fail fast when Chrome never binds CDP
    return chrome


def stop_chrome(chrome: Chrome) -> None:
    chrome.proc.terminate()
    with contextlib.suppress(Exception):
        chrome.proc.wait(timeout=10)


@pytest.fixture(scope="session")
def chrome(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Chrome]:
    profile = tmp_path_factory.mktemp("chrome-profile")
    instance = launch_chrome(profile)
    try:
        yield instance
    finally:
        stop_chrome(instance)
