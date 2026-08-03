"""Command-line entry point for MiniRec."""

from __future__ import annotations

import sys


def main() -> int:
    from .application import MiniRecApplication

    return MiniRecApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
