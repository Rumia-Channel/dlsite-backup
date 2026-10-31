"""Minimal Chrome DevTools Protocol client.

Only the handful of commands needed to open a login window and read the
cookies the browser ends up holding are implemented. There is no browser
automation layer here on purpose: the user drives the browser themselves, so
two-factor authentication and CAPTCHA keep working as they do in a normal
browser.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.client import connect


class CdpError(RuntimeError):
    """A DevTools command returned an error."""


class DevToolsClient:
    """Client for the DevTools *browser* endpoint.

    The browser endpoint (``/devtools/browser/...``) is required for
    ``Storage.getCookies``: the per-page endpoints cannot read the whole
    cookie store.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._ws: Any = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "DevToolsClient":
        self._ws = await connect(self._url, max_size=None)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a DevTools command and return its result."""
        async with self._lock:
            if self._ws is None:
                raise CdpError("not connected")
            self._next_id += 1
            message_id = self._next_id
            message: dict[str, Any] = {
                "id": message_id,
                "method": method,
                "params": params or {},
            }
            if session_id is not None:
                message["sessionId"] = session_id
            await self._ws.send(json.dumps(message))
            while True:
                reply = json.loads(await self._ws.recv())
                if reply.get("id") != message_id:
                    # An event, or a reply to a command we are not waiting for.
                    continue
                if "error" in reply:
                    raise CdpError(f"{method} failed: {reply['error']}")
                return reply.get("result", {})

    async def attach_to_target(self, target_id: str) -> str:
        """Attach to a tab and return the session id used for its messages."""
        result = await self.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        return str(result["sessionId"])

    async def evaluate(self, session_id: str, expression: str) -> Any:
        """Evaluate JavaScript in an attached tab and return its value."""
        result = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=session_id,
        )
        if result.get("exceptionDetails"):
            raise CdpError(f"evaluation failed: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    async def get_cookies(self) -> list[dict[str, Any]]:
        """Return every cookie the browser holds."""
        result = await self.send("Storage.getCookies")
        return list(result.get("cookies", []))

    async def create_target(self, url: str) -> str:
        """Open a new tab and return its target id."""
        result = await self.send("Target.createTarget", {"url": url})
        return str(result["targetId"])

    async def target_url(self, target_id: str) -> str:
        """Return the URL a tab is currently showing."""
        result = await self.send("Target.getTargetInfo", {"targetId": target_id})
        return str(result.get("targetInfo", {}).get("url", ""))

    async def close_target(self, target_id: str) -> None:
        """Close a tab."""
        await self.send("Target.closeTarget", {"targetId": target_id})

    async def close_browser(self) -> None:
        """Ask the browser to shut down."""
        try:
            await self.send("Browser.close")
        except CdpError:
            pass
