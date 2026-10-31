"""Run the login step in the user's own browser.

The browser is launched with a remote debugging port and its DevTools
endpoint is asked for the cookies once the user has logged in. Everything the
site puts in front of a login (Cloudflare's challenge, two-factor
authentication, CAPTCHA) is handled by the browser itself, so no part of that
flow has to be re-implemented here.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from websockets.exceptions import ConnectionClosedError

from .cdp import CdpError, DevToolsClient
from .config import LOGIN_URL

DEVTOOLS_ACTIVE_PORT_FILE = "DevToolsActivePort"


class BrowserNotFoundError(RuntimeError):
    """No Chromium-family browser could be found."""


class LoginTimeoutError(RuntimeError):
    """The login did not finish in time."""


class BrowserClosedError(RuntimeError):
    """The browser went away before the cookies could be read."""


def browser_candidates() -> list[Path]:
    """Return the paths a Chromium-family browser may be installed at."""
    roots: list[Path] = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value))
    relatives = [
        Path("Google/Chrome/Application/chrome.exe"),
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("BraveSoftware/Brave-Browser/Application/brave.exe"),
        Path("Chromium/Application/chrome.exe"),
    ]
    candidates = [root / rel for root in roots for rel in relatives]
    candidates += [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/microsoft-edge"),
    ]
    return candidates


def find_browser(explicit: str | None = None) -> Path:
    """Return the browser to use, preferring an explicitly given path."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise BrowserNotFoundError(f"{path} is not a file")
        return path
    for candidate in browser_candidates():
        if candidate.is_file():
            return candidate
    raise BrowserNotFoundError(
        "no Chromium-family browser found; pass one with --browser"
    )


def launch_browser(browser: Path, profile: Path) -> subprocess.Popen[bytes]:
    """Start the browser on a free debugging port and return the process.

    The browser picks the port itself and writes it into
    ``<profile>/DevToolsActivePort``.
    """
    profile.mkdir(parents=True, exist_ok=True)
    # A leftover file from an earlier run would point at a dead browser.
    (profile / DEVTOOLS_ACTIVE_PORT_FILE).unlink(missing_ok=True)
    command = [
        str(browser),
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    return subprocess.Popen(  # noqa: S603 - the path is resolved by find_browser
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def read_devtools_endpoint(
    profile: Path, timeout: float = 30.0
) -> tuple[int, str]:
    """Wait for the browser to publish its DevTools endpoint.

    Returns the port and the browser websocket path.
    """
    path = profile / DEVTOOLS_ACTIVE_PORT_FILE
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            lines = []
        if len(lines) >= 2:
            try:
                port = int(lines[0].strip())
            except ValueError:
                port = 0
            websocket = lines[1].strip()
            if port > 0 and websocket.startswith("/devtools/browser/"):
                return port, websocket
        await asyncio.sleep(0.2)
    raise LoginTimeoutError("the browser did not open a DevTools endpoint")


class _EnterWaiter:
    """Waits for the user to press Enter on the terminal."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not sys.stdin.isatty():
            # Nothing to read from; the caller relies on the other signals.
            return

        def wait() -> None:
            try:
                line = sys.stdin.readline()
            except Exception:  # noqa: BLE001 - a closed stdin is not a key press
                return
            # An empty string means end of input, not a key press.
            if line:
                self._event.set()

        self._thread = threading.Thread(target=wait, daemon=True)
        self._thread.start()

    def triggered(self) -> bool:
        return self._event.is_set()


async def wait_for_login(
    client: DevToolsClient,
    target_id: str,
    timeout: float,
    authenticated: Any = None,
    site_filter: Any = None,
) -> None:
    """Wait until the user says the login is done.

    The browser is only closed after the user presses Enter on the terminal,
    so the login window stays open for as long as it is needed. The optional
    ``authenticated`` callback is only used to tell the user that the login
    looks complete; it never ends the wait on its own.
    """
    waiter = _EnterWaiter()
    waiter.start()
    deadline = time.monotonic() + timeout
    signature: tuple[tuple[str, str], ...] | None = None
    next_probe = 0.0
    announced = False
    while time.monotonic() < deadline:
        cookies = await client.get_cookies()
        relevant = [
            cookie
            for cookie in cookies
            if site_filter is None or site_filter(cookie)
        ]
        if relevant and authenticated is not None and not announced:
            current = tuple(
                sorted(
                    (str(c.get("name", "")), str(c.get("domain", ""))) for c in cookies
                )
            )
            if current != signature or time.monotonic() >= next_probe:
                signature = current
                next_probe = time.monotonic() + 30.0
                if await authenticated(cookies):
                    announced = True
                    print(
                        "ログインを確認しました。ブラウザを閉じてもよければ "
                        "この画面で Enter キーを押してください。",
                        flush=True,
                    )
        if waiter.triggered():
            return
        await asyncio.sleep(1.0)
    raise LoginTimeoutError("the login was not completed in time")


async def wait_for_page(
    client: DevToolsClient, target_id: str, expected: str, timeout: float = 60.0
) -> None:
    """Wait until a tab has actually navigated to the expected page."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = await client.target_url(target_id)
        if expected in url:
            return
        await asyncio.sleep(0.5)
    raise LoginTimeoutError(f"{expected} did not load in the browser")


async def collect_login_cookies(
    browser: Path,
    profile: Path,
    timeout: float = 300.0,
    login_url: str = LOGIN_URL,
    authenticated: Any = None,
    site_filter: Any = None,
) -> list[dict[str, Any]]:
    """Open the login page in the browser and return the cookies it holds.

    When the browser is closed before the cookies could be read, the session
    is still in the browser profile, so it is read from a fresh instance of
    the same profile.
    """
    try:
        return await _login_in_browser(
            browser, profile, timeout, login_url, authenticated, site_filter
        )
    except BrowserClosedError:
        print(
            "ブラウザが閉じられたため、プロフィールから Cookie を読み直します。",
            flush=True,
        )
        return await _read_profile_cookies(browser, profile)


async def _login_in_browser(
    browser: Path,
    profile: Path,
    timeout: float,
    login_url: str,
    authenticated: Any,
    site_filter: Any,
) -> list[dict[str, Any]]:
    """Show the login page and collect the cookies once the user is done."""
    process = launch_browser(browser, profile)
    try:
        port, websocket = await read_devtools_endpoint(profile)
        url = f"ws://127.0.0.1:{port}{websocket}"
        async with DevToolsClient(url) as client:
            target_id = await client.create_target(login_url)
            print(
                f"ブラウザで {login_url} を開きました。\n"
                "ログインが完了したら、この画面で Enter キーを押してください。\n"
                "（Enter を押すまでブラウザは閉じません）",
                flush=True,
            )
            try:
                await wait_for_page(client, target_id, "login.dlsite.com")
                await wait_for_login(
                    client, target_id, timeout, authenticated, site_filter
                )
                # Read the cookies before closing anything: a browser that is
                # on its way out cannot answer any more.
                cookies = await client.get_cookies()
            except (ConnectionClosedError, CdpError, EOFError) as exc:
                raise BrowserClosedError("the browser was closed") from exc
            await _close_quietly(client.close_target(target_id))
            await _close_quietly(client.close_browser())
        return cookies
    finally:
        terminate_browser(process)


async def _read_profile_cookies(browser: Path, profile: Path) -> list[dict[str, Any]]:
    """Read the cookies a browser profile holds."""
    process = launch_browser(browser, profile)
    try:
        port, websocket = await read_devtools_endpoint(profile)
        url = f"ws://127.0.0.1:{port}{websocket}"
        async with DevToolsClient(url) as client:
            # Give the browser a moment to restore its session.
            await asyncio.sleep(2.0)
            cookies = await client.get_cookies()
            await _close_quietly(client.close_browser())
        return cookies
    except (ConnectionClosedError, CdpError, EOFError) as exc:
        raise BrowserClosedError("the browser profile could not be read") from exc
    finally:
        terminate_browser(process)


async def _close_quietly(coroutine: Any) -> None:
    """Await a cleanup call and ignore a browser that is already gone."""
    try:
        await coroutine
    except (ConnectionClosedError, CdpError, EOFError):
        pass


def terminate_browser(process: subprocess.Popen[bytes]) -> None:
    """Stop the browser and everything it started."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(  # noqa: S603,S607 - fixed command, own process id
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - stubborn browser
        process.kill()
