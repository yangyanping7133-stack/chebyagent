#!/usr/bin/env python3
"""Production entrypoint that keeps unexpected tracebacks out of container logs."""

from __future__ import annotations

import os
import sys


def run() -> int:
    # GatewayStore creates its SQLite database, WAL and shared-memory files.
    # Establish the private process mask before importing any application code.
    os.umask(0o077)
    try:
        from cheby_connector.main import main

        main()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("ERROR Connector stopped unexpectedly", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
