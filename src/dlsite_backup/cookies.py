"""Session cookie storage.

The cookies are the DLsite session, so they are kept encrypted at rest: the
value on disk is protected with a key that lives in a separate file, and the
site name is bound into the authentication tag so a ciphertext cannot be
replayed for a different site.
"""

from __future__ import annotations

import base64
import json
import os
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from .config import SITE_DOMAIN

#: Marker of a value that is encrypted at rest.
AT_REST_PREFIX = "enc:v1:"

#: Associated data: binds a ciphertext to this site.
AAD = b"dlsite-backup/login-cookie"

KEY_LEN = 32
NONCE_LEN = 12


class SessionError(RuntimeError):
    """The stored session could not be read."""


# --- key handling ---------------------------------------------------------


def load_or_create_key(path: Path) -> bytes:
    """Return the local encryption key, creating it on first use."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        key = os.urandom(KEY_LEN)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(base64.b64encode(key).decode("ascii"), encoding="utf-8")
        _restrict_permissions(path)
        return key
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001 - any decode problem is fatal here
        raise SessionError(f"{path} does not hold a valid base64 key") from exc
    if len(key) != KEY_LEN:
        raise SessionError(f"{path} holds a {len(key)} byte key, expected {KEY_LEN}")
    return key


def _restrict_permissions(path: Path) -> None:
    """Best-effort 0600 on POSIX; a no-op on Windows."""
    if os.name != "posix":
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass


# --- encryption -----------------------------------------------------------


def encrypt(key: bytes, plaintext: bytes, aad: bytes = AAD) -> str:
    """Encrypt a value and return its ``enc:v1:`` representation."""
    nonce = os.urandom(NONCE_LEN)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return (
        AT_REST_PREFIX
        + base64.b64encode(nonce).decode("ascii")
        + ":"
        + base64.b64encode(ciphertext).decode("ascii")
    )


def decrypt(key: bytes, token: str, aad: bytes = AAD) -> bytes:
    """Decrypt an ``enc:v1:`` value."""
    if not token.startswith(AT_REST_PREFIX):
        raise SessionError("value is not encrypted")
    body = token[len(AT_REST_PREFIX) :]
    nonce_b64, _, ciphertext_b64 = body.partition(":")
    try:
        nonce = base64.b64decode(nonce_b64, validate=True)
        ciphertext = base64.b64decode(ciphertext_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SessionError("encrypted value is malformed") from exc
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:  # noqa: BLE001
        raise SessionError("could not decrypt the stored session") from exc


# --- cookie representation ------------------------------------------------


def cookie_dict(
    name: str,
    value: str,
    domain: str,
    path: str = "/",
    secure: bool = False,
    http_only: bool = False,
    expires: int | None = None,
) -> dict[str, Any]:
    """Build one cookie record."""
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "secure": secure,
        "httpOnly": http_only,
        "expires": expires,
    }


def in_site_family(domain: str, site: str = SITE_DOMAIN) -> bool:
    """Return whether a cookie domain belongs to the site's domain family.

    DLsite keeps its session across ``login.dlsite.com``, ``www.dlsite.com``,
    ``play.dlsite.com`` and ``play.dl.dlsite.com``, so the whole family is
    kept together as one login.
    """
    domain = domain.lstrip(".").lower()
    site = site.lstrip(".").lower()
    return domain == site or domain.endswith("." + site)


def filter_site(cookies: list[dict[str, Any]], site: str = SITE_DOMAIN) -> list[dict[str, Any]]:
    """Return only the cookies belonging to the site's domain family."""
    return [c for c in cookies if in_site_family(str(c.get("domain", "")), site)]


def to_cookiejar(cookies: list[dict[str, Any]]) -> CookieJar:
    """Convert cookie records into a domain-scoped cookie jar."""
    jar = CookieJar()
    for record in cookies:
        domain = str(record.get("domain", ""))
        host_only = not domain.startswith(".")
        expires = record.get("expires")
        jar.set_cookie(
            Cookie(
                version=0,
                name=str(record["name"]),
                value=str(record["value"]),
                port=None,
                port_specified=False,
                # The leading dot is kept: it is what tells the jar that the
                # cookie also covers the subdomains of the domain.
                domain=domain,
                domain_specified=not host_only,
                domain_initial_dot=not host_only,
                path=str(record.get("path") or "/"),
                path_specified=True,
                secure=bool(record.get("secure")),
                expires=int(expires) if expires and int(expires) > 0 else None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""} if record.get("httpOnly") else {},
                rfc2109=False,
            )
        )
    return jar


def from_cookiejar(jar: CookieJar) -> list[dict[str, Any]]:
    """Convert a cookie jar into cookie records."""
    records: list[dict[str, Any]] = []
    for cookie in jar:
        records.append(
            {
                "name": cookie.name,
                "value": cookie.value or "",
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
                "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly")),
                "expires": cookie.expires,
            }
        )
    return records


def from_cdp(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert DevTools cookie records into cookie records."""
    records: list[dict[str, Any]] = []
    for cookie in cookies:
        expires = cookie.get("expires")
        records.append(
            cookie_dict(
                name=str(cookie.get("name", "")),
                value=str(cookie.get("value", "")),
                domain=str(cookie.get("domain", "")),
                path=str(cookie.get("path") or "/"),
                secure=bool(cookie.get("secure")),
                http_only=bool(cookie.get("httpOnly")),
                expires=int(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
            )
        )
    return records


# --- session file ---------------------------------------------------------


def save_session(path: Path, key: bytes, cookies: list[dict[str, Any]]) -> None:
    """Write the cookies to disk, encrypted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"site": SITE_DOMAIN, "cookies": cookies}, ensure_ascii=False
    ).encode("utf-8")
    path.write_text(encrypt(key, payload), encoding="utf-8")
    _restrict_permissions(path)


def load_session(path: Path, key: bytes) -> list[dict[str, Any]]:
    """Read the cookies back from disk."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise SessionError(
            "no stored session; run `dlsite-backup login` first"
        ) from exc
    payload = json.loads(decrypt(key, token).decode("utf-8"))
    return list(payload.get("cookies", []))
