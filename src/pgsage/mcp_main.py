from __future__ import annotations

import sys


def main() -> None:
    try:
        from pgsage.mcp_server import main as serve
    except ImportError as error:
        sys.stderr.write(
            f"error: pgsage-mcp needs the 'mcp' extra ({error.name} is missing): "
            "pip install 'pgsage[mcp]'\n"
        )
        raise SystemExit(2) from error
    serve()
