"""Allow ``python -m dlsite_backup``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
