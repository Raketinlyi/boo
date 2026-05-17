from __future__ import annotations

import json
import logging
import time
from typing import Iterable, Optional

import aiohttp

from utils.symbols import split_pair_symbol, normalize_pair_symbol
from ws_server.adapters.base import WsAdapter
from ws_server.models import Quote
from ws_server.store import QuoteStore


class KrakenProBookAdapter(WsAdapter):
    name = "Kraken Pro"
    url = "wss://ws.kraken.com/v2"

    def __init__(self, store: QuoteStore, symbols: Iterable[str], *, batch_size: int = 60) -> None:
        super().__init__(store, symbols)
        self.batch_size = max(1, int(batch_size or 60))

    @staticmethod
    def _ws_symbol(symbol: str) -> str:
        base, quote = split_pair_symbol(symbol)
        if base == "BTC":
            base = "XBT"
        return f"{base}/{quote}"

    async def run(self) -> None:
        if not self.symbols:
            logging.warning("Kraken Pro WS: no symbols to subscribe")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url, heartbeat=20, receive_timeout=60) as ws:
                        await self._subscribe(ws)
                        logging.info("Kraken Pro WS connected (%s symbols)", len(self.symbols))
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
        symbols = [self._ws_symbol(s) for s in self.symbols]
        for i in range(0, len(symbols), self.batch_size):
            await ws.send_json({"method": "subscribe", "params": {"channel": "book", "symbol": symbols[i:i+self.batch_size], "depth": 10, "snapshot": True}})
            await self.sleep_or_stop(0.05)

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict) or msg.get("channel") != "book":
            return
        rows = msg.get("data") or []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            symbol_raw = row.get("symbol")
            symbol = self._normalize_ws_symbol(symbol_raw)
            bids = row.get("bids") or []
            asks = row.get("asks") or []
            bid0 = bids[0] if bids else None
            ask0 = asks[0] if asks else None
            bid = self._level_price(bid0)
            ask = self._level_price(ask0)
            if not symbol or not bid or not ask:
                continue
            await self.store.upsert(Quote(exchange=self.name, symbol=symbol, bid=bid, ask=ask,
                                          bid_size=self._level_qty(bid0), ask_size=self._level_qty(ask0),
                                          ts=time.time(), source="ws:krakenpro.book"))

    @staticmethod
    def _normalize_ws_symbol(s: str) -> str:
        if not s:
            return ""
        base, quote = split_pair_symbol(str(s).replace("XBT", "BTC"))
        return normalize_pair_symbol(f"{base}{quote}")

    @staticmethod
    def _level_price(level) -> Optional[float]:
        try:
            if isinstance(level, dict):
                v = level.get("price")
            else:
                v = level[0]
            out = float(v)
            return out if out > 0 else None
        except Exception:
            return None

    @staticmethod
    def _level_qty(level) -> Optional[float]:
        try:
            if isinstance(level, dict):
                v = level.get("qty") or level.get("size")
            else:
                v = level[1]
            out = float(v)
            return out if out > 0 else None
        except Exception:
            return None
