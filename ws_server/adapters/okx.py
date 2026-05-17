from __future__ import annotations

import json
import logging
import time
from typing import Iterable, Optional

import aiohttp

from utils.symbols import format_pair_symbol, normalize_pair_symbol
from ws_server.adapters.base import WsAdapter
from ws_server.models import Quote
from ws_server.store import QuoteStore


class OkxTickerAdapter(WsAdapter):
    name = "OKX"
    url = "wss://ws.okx.com:8443/ws/v5/public"

    def __init__(self, store: QuoteStore, symbols: Iterable[str], *, batch_size: int = 80) -> None:
        super().__init__(store, symbols)
        self.batch_size = max(1, int(batch_size or 80))

    async def run(self) -> None:
        if not self.symbols:
            logging.warning("OKX WS: no symbols to subscribe")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url, heartbeat=20, receive_timeout=60) as ws:
                        await self._subscribe(ws)
                        logging.info("OKX WS connected (%s symbols)", len(self.symbols))
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
        args = [{"channel": "tickers", "instId": format_pair_symbol(sym, sep="-")} for sym in self.symbols]
        for i in range(0, len(args), self.batch_size):
            await ws.send_json({"op": "subscribe", "args": args[i : i + self.batch_size]})

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        rows = data.get("data")
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = normalize_pair_symbol(row.get("instId"))
            bid = self._to_float(row.get("bidPx"))
            ask = self._to_float(row.get("askPx"))
            last = self._to_float(row.get("last"))
            if not symbol or not (bid or ask or last):
                continue
            await self.store.upsert(
                Quote(
                    exchange=self.name,
                    symbol=symbol,
                    bid=bid,
                    ask=ask,
                    bid_size=self._to_float(row.get("bidSz")),
                    ask_size=self._to_float(row.get("askSz")),
                    last=last,
                    ts=time.time(),
                    source="ws:okx.tickers",
                )
            )

    @staticmethod
    def _to_float(value) -> Optional[float]:
        try:
            out = float(value)
            return out if out > 0 else None
        except (TypeError, ValueError):
            return None
