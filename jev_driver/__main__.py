"""`python -m jev_driver` runs the drive CLI (same entry as the `wwwdrive` script)."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
