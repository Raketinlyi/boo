from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from ws_server.store import QuoteStore


class WsAdapter:
    name = "base"

    def __init__(self, store: QuoteStore, symbols: Iterable[str]) -> None:
        self.store = store
        self.symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
        self._stop = asyncio.Event()

    async def run(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        self._stop.set()

    async def sleep_or_stop(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    def log_exception(self, message: str, exc: BaseException) -> None:
        logging.warning("%s: %s: %s", self.name, message, exc)
