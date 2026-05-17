from __future__ import annotations

import argparse
import logging

from aiohttp import web

from config import Config
from ws_server.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone WebSocket-first arbitrage server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    args = parser.parse_args()

    # Compact one-line format (matches run_unified.setup_logging). The WS
    # process has its own stdout/stderr which run_unified redirects to
    # logs/ws_server.out / .err — see run_unified.start_ws_market_server_if_needed.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence access-log flood and other low-signal third-party chatter.
    for noisy in (
        "aiohttp.access",
        "aiohttp.client",
        "aiohttp.server",
        "asyncio",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    app = create_app(Config())
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
