"""Allow running sessfork directly via `python3 -m sessfork`."""

from __future__ import annotations

import sys

from sessfork.cli import main

if __name__ == "__main__":
    sys.exit(main())
