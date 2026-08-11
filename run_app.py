"""Entry point for the Origin desktop app (used by launchers and PyInstaller)."""

from origin.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
