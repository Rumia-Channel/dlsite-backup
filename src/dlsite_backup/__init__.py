"""Back up DLsite Play purchases to local storage."""

from __future__ import annotations


def main() -> None:
    """Entry point of the ``dlsite-backup`` command."""
    from .cli import main as _main

    raise SystemExit(_main())


__all__ = ["main"]
