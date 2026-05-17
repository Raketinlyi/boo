from __future__ import annotations

import json
import logging
import time
from typing import Iterable, Optional

import aiohttp

from utils.symbols import normalize_pair_symbol
from ws_server.adapters.base import WsAdapter
from ws_server.models import Quote
from ws_server.store import QuoteStore


class BinanceUsBookTickerAdapter(WsAdapter):
    name = "Binance.US"
    base_url = "wss://stream.binance.us:9443/stream?streams="

    def __init__(self, store: QuoteStore, symbols: Iterable[str], *, batch_size: int = 80) -> None:
        super().__init__(store, symbols)
        self.batch_size = max(1, int(batch_size or 80))

    async def run(self) -> None:
        if not self.symbols:
            logging.warning("Binance.US WS: no symbols to subscribe")
            return
        # Binance.US combined streams use URL path, so shard by URL length/topic count.
        tasks = []
        for i in range(0, len(self.symbols), self.batch_size):
            shard = self.symbols[i:i+self.batch_size]
            tasks.append(self._run_shard(shard))
        await asyncio_gather_stop_safe(tasks)

    async def _run_shard(self, symbols):
        streams = "/".join(f"{s.lower()}@bookTicker" for s in symbols)
        url = self.base_url + streams
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=20, receive_timeout=60) as ws:
                        logging.info("Binance.US WS connected (%s symbols)", len(symbols))
                        backoff = 1.0
                        async for msg in ws:
                            if self._stop.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except Exception as exc:
                self.log_exception("connection dropped", exc)
            if await self.sleep_or_stop(backoff):
                break
            backoff = min(30.0, backoff * 2.0)

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        data = msg.get("data") if isinstance(msg.get("data"), dict) else msg
        symbol = normalize_pair_symbol(data.get("s"))
        bid = self._to_float(data.get("b"))
        ask = self._to_float(data.get("a"))
        if not symbol or not bid or not ask:
            return
        await self.store.upsert(Quote(exchange=self.name, symbol=symbol, bid=bid, ask=ask,
                                      bid_size=self._to_float(data.get("B")), ask_size=self._to_float(data.get("A")),
                                      ts=time.time(), source="ws:binanceus.bookTicker"))

    @staticmethod
    def _to_float(value) -> Optional[float]:
        try:
            out = float(value)
            return out if out > 0 else None
        except (TypeError, ValueError):
            return None


async def asyncio_gather_stop_safe(coros):
    import asyncio
    if not coros:
        return
    await asyncio.gather(*coros, return_exceptions=True)
