"""Turn stored cookies into a usable DLsite Play session."""

from __future__ import annotations

from typing import Any

from curl_cffi import requests as curl_requests
from dlsite_async import PlayAPI

from .config import (
    PLAY_AUTHORIZE_URL,
    PLAY_CONTENT_COUNT_URL,
    PLAY_LOGIN_URL,
    PLAY_REFERER,
)
from .cookies import from_cookiejar, to_cookiejar


def build_play_api(cookies: list[dict[str, Any]]) -> PlayAPI:
    """Return a Play API session that carries the stored cookies.

    The session is not logged in by this function: the cookies already hold a
    logged-in DLsite session, obtained through the browser.
    """
    return PlayAPI(cookies=to_cookiejar(cookies))


async def establish_play_session(
    cookies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Visit the Play endpoints once so the Play session is issued.

    DLsite Play keeps part of its session in cookies that are only handed out
    to a request that comes from the Play site, so both endpoints are visited
    with a matching Referer.

    Returns the merged cookie set.
    """
    jar = to_cookiejar(cookies)
    async with curl_requests.AsyncSession(
        impersonate="chrome", cookies=jar
    ) as client:
        await client.get(PLAY_LOGIN_URL)
        await client.get(PLAY_AUTHORIZE_URL, headers={"Referer": PLAY_REFERER})
    return from_cookiejar(jar)


async def session_works(cookies: list[dict[str, Any]]) -> bool:
    """Return whether the cookies give access to the Play API.

    This is used to recognise a finished login without guessing: the Play
    API answers with 200 only for a session that is really logged in.
    """
    try:
        merged = merge_cookies(cookies, await establish_play_session(cookies))
        async with curl_requests.AsyncSession(
            impersonate="chrome", cookies=to_cookiejar(merged)
        ) as client:
            response = await client.get(PLAY_CONTENT_COUNT_URL)
            return response.status_code == 200
    except Exception:  # noqa: BLE001 - a failed probe just means "not yet"
        return False


def merge_cookies(
    *cookie_sets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge cookie sets, letting later sets win on name and domain."""
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cookies in cookie_sets:
        for cookie in cookies:
            key = (
                str(cookie.get("name", "")),
                str(cookie.get("domain", "")).lstrip("."),
                str(cookie.get("path", "/")),
            )
            merged[key] = cookie
    return list(merged.values())
