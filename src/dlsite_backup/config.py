"""Paths, URLs and other fixed values for dlsite-backup."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "dlsite-backup"

#: Domain family that holds every cookie worth keeping.
SITE_DOMAIN = "dlsite.com"

#: Where the browser login flow starts.
LOGIN_URL = "https://login.dlsite.com/login"

#: DLsite Play endpoints that have to be visited once to establish the
#: Play session. ``/api/authorize`` only accepts a request that carries a
#: Referer from the Play site, so it is sent with an explicit header.
PLAY_LOGIN_URL = "https://play.dlsite.com/login/"
PLAY_AUTHORIZE_URL = "https://play.dlsite.com/api/authorize"
PLAY_REFERER = "https://play.dlsite.com/"
PLAY_CONTENT_COUNT_URL = "https://play.dlsite.com/api/v3/content/count"

DEFAULT_HOME = Path.home() / f".{APP_NAME}"
KEY_FILE_NAME = "login.key"
SESSION_FILE_NAME = "session.json"
PROFILE_DIR_NAME = "browser-profile"


def default_home() -> Path:
    """Return the directory holding the session cookies and browser profile."""
    override = os.environ.get("DLSITE_BACKUP_HOME")
    if override:
        return Path(override).expanduser()
    return DEFAULT_HOME


def key_path(home: Path | None = None) -> Path:
    """Return the path of the local encryption key."""
    return (home or default_home()) / KEY_FILE_NAME


def session_path(home: Path | None = None) -> Path:
    """Return the path of the stored (encrypted) session cookies."""
    return (home or default_home()) / SESSION_FILE_NAME


def profile_path(home: Path | None = None) -> Path:
    """Return the path of the browser profile used for the login flow."""
    return (home or default_home()) / PROFILE_DIR_NAME
