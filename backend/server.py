"""Run with a known socket so local opt-in cannot mistake a public bind for loopback."""
from __future__ import annotations

import argparse
import os

import uvicorn

from backend import security


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("UVICORN_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", os.getenv("UVICORN_PORT", "8000"))))
    args = parser.parse_args()
    config = uvicorn.Config("backend.main:app", host=args.host, port=args.port)
    # Bind first, then record the OS-resolved address, never a Host/forwarded header.
    with config.bind_socket() as sock:
        security._server_bind_host = sock.getsockname()[0]
        try:
            server = uvicorn.Server(config)
            server.run(sockets=[sock])
            if not server.started:
                raise SystemExit(3)
        finally:
            security._server_bind_host = None


if __name__ == "__main__":
    main()
