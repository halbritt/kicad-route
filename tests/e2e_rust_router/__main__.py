"""CLI entrypoint for `python3 -m tests.e2e_rust_router`."""

from __future__ import annotations

import sys

from tests.e2e_rust_router.runner import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
