from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Iterable, Optional

import aiohttp

from utils.symbols import format_pair_symbol, normalize_pair_symbol
from ws_server.adapters.base import WsAdapter
from ws_server.models import Quote
from ws_server.store import QuoteStore


class GateIoBookTickerAdapter(WsAdapter):
    """Gate.io spot.book_ticker listener.

    Official public WS endpoint: wss://api.gateio.ws/ws/v4/
    Channel: spot.book_ticker
    """

    name = "Gate.io"
    url = "wss://api.gateio.ws/ws/v4/"

    def __init__(self, store: QuoteStore, symbols: Iterable[str], *, batch_size: int = 80) -> None:
        super().__init__(store, symbols)
        self.batch_size = max(1, int(batch_size or 80))

    @staticmethod
    def _raw_symbol(symbol: str) -> str:
        return format_pair_symbol(symbol, sep="_")

    async def run(self) -> None:
        if not self.symbols:
            logging.warning("Gate.io WS: no symbols to subscribe")
            return

        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url, heartbeat=20, receive_timeout=60) as ws:
                        await self._subscribe(ws)
                        logging.info("Gate.io WS connected (%s symbols)", len(self.symbols))
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

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        raw_symbols = [self._raw_symbol(sym) for sym in self.symbols]
        for i in range(0, len(raw_symbols), self.batch_size):
            payload = raw_symbols[i : i + self.batch_size]
            await ws.send_json(
                {
                    "time": int(time.time()),
                    "channel": "spot.book_ticker",
                    "event": "subscribe",
                    "payload": payload,
                }
            )
            await asyncio.sleep(0.05)

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        if data.get("channel") != "spot.book_ticker":
            return
        result = data.get("result")
        if not isinstance(result, dict):
            return

        raw_symbol = result.get("s") or result.get("currency_pair")
        symbol = normalize_pair_symbol(raw_symbol)
        bid = self._to_float(result.get("b") or result.get("highest_bid"))
        ask = self._to_float(result.get("a") or result.get("lowest_ask"))
        bid_size = self._to_float(result.get("B") or result.get("highest_size"))
        ask_size = self._to_float(result.get("A") or result.get("lowest_size"))
        if not symbol or not bid or not ask:
            return

        await self.store.upsert(
            Quote(
                exchange=self.name,
                symbol=symbol,
                bid=bid,
                ask=ask,
                bid_size=bid_size,
                ask_size=ask_size,
                last=None,
                ts=time.time(),
                source="ws:gateio.book_ticker",
            )
        )

    @staticmethod
    def _to_float(value) -> Optional[float]:
        try:
            out = float(value)
            return out if out > 0 else None
        except (TypeError, ValueError):
            return None
