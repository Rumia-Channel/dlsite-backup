"""Where the backup lives on disk and what is recorded about it."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

WORK_FILENAME = "work.json"
STATE_FILENAME = "state.json"
WORKS_DIRNAME = "works"
PLAYFILES_DIRNAME = "playfiles"
VIEWER_DIRNAME = "viewer"
EPUB_DIRNAME = "epub"


class UnsafePathError(ValueError):
    """A path from the site tried to escape the backup directory."""


def safe_relative_path(relative: str) -> PurePosixPath:
    """Return a relative path that cannot escape its parent directory.

    The paths come from the site, so they are treated as untrusted input.
    """
    candidate = PurePosixPath(relative.replace("\\", "/"))
    if candidate.is_absolute() or relative.startswith("/"):
        raise UnsafePathError(f"{relative!r} is not a relative path")
    parts = [part for part in candidate.parts if part not in (".",)]
    if any(part == ".." for part in parts):
        raise UnsafePathError(f"{relative!r} tries to escape the backup directory")
    if not parts:
        raise UnsafePathError(f"{relative!r} is empty")
    return PurePosixPath(*parts)


def _jsonable(value: Any) -> Any:
    """Convert dataclasses, enums and dates into JSON friendly values."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class WorkStore:
    """The on-disk layout of a backup directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # --- layout ----------------------------------------------------------

    @property
    def works_root(self) -> Path:
        """Return the directory holding one subdirectory per work."""
        return self.root / WORKS_DIRNAME

    def work_dir(self, product_id: str) -> Path:
        """Return the directory of a single work."""
        return self.works_root / safe_relative_path(product_id).__str__()

    def playfiles_dir(self, product_id: str) -> Path:
        """Return the directory for files taken straight from the ziptree."""
        return self.work_dir(product_id) / PLAYFILES_DIRNAME

    def viewer_dir(self, product_id: str, stem: str) -> Path:
        """Return the directory for pages coming out of a viewer session."""
        return self.work_dir(product_id) / VIEWER_DIRNAME / safe_relative_path(stem).__str__()

    def epub_dir(self, product_id: str) -> Path:
        """Return the directory for reconstructed EPUB archives."""
        return self.work_dir(product_id) / EPUB_DIRNAME

    def playfile_path(self, product_id: str, relative: str) -> Path:
        """Return the path a ziptree file is stored at."""
        return self.playfiles_dir(product_id) / safe_relative_path(relative).__str__()

    # --- metadata --------------------------------------------------------

    def write_metadata(self, product_id: str, work: Any, purchase_date: Any = None) -> Path:
        """Write the work metadata next to its files."""
        payload = {
            "product_id": product_id,
            "purchase_date": _jsonable(purchase_date),
            "work": _jsonable(work),
        }
        path = self.work_dir(product_id) / WORK_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def write_state(self, product_id: str, state: dict[str, Any]) -> Path:
        """Write the backup state of a work."""
        path = self.work_dir(product_id) / STATE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(state)
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def read_state(self, product_id: str) -> dict[str, Any]:
        """Read the backup state of a work."""
        path = self.work_dir(product_id) / STATE_FILENAME
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def stored_work_ids(self) -> list[str]:
        """Return the product ids that already have a directory."""
        if not self.works_root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.works_root.iterdir()
            if entry.is_dir() and (entry / STATE_FILENAME).exists()
        )


def relative_to(path: Path, root: Path) -> str:
    """Return a path relative to the backup root, using forward slashes."""
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:  # pragma: no cover - different drives on Windows
        return str(path)
