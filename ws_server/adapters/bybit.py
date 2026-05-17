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


class BybitTickerAdapter(WsAdapter):
    name = "Bybit"
    url = "wss://stream.bybit.com/v5/public/spot"

    def __init__(self, store: QuoteStore, symbols: Iterable[str], *, batch_size: int = 10) -> None:
        super().__init__(store, symbols)
        self.batch_size = max(1, int(batch_size or 10))

    async def run(self) -> None:
        if not self.symbols:
            logging.warning("Bybit WS: no symbols to subscribe")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url, heartbeat=20, receive_timeout=60) as ws:
                        await self._subscribe(ws)
                        logging.info("Bybit WS connected (%s symbols)", len(self.symbols))
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
        topics = [f"orderbook.1.{sym}" for sym in self.symbols]
        for i in range(0, len(topics), self.batch_size):
            await ws.send_json({"op": "subscribe", "args": topics[i : i + self.batch_size]})
            await self.sleep_or_stop(0.05)

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        row = msg.get("data")
        if not isinstance(row, dict):
            return
        symbol = normalize_pair_symbol(row.get("s"))
        bids = row.get("b") or []
        asks = row.get("a") or []
        best_bid = bids[0] if bids and isinstance(bids[0], list) else []
        best_ask = asks[0] if asks and isinstance(asks[0], list) else []
        bid = self._to_float(best_bid[0] if len(best_bid) > 0 else None)
        ask = self._to_float(best_ask[0] if len(best_ask) > 0 else None)
        if not symbol or not (bid or ask):
            return
        await self.store.upsert(
            Quote(
                exchange=self.name,
                symbol=symbol,
                bid=bid,
                ask=ask,
                bid_size=self._to_float(best_bid[1] if len(best_bid) > 1 else None),
                ask_size=self._to_float(best_ask[1] if len(best_ask) > 1 else None),
                last=None,
                ts=time.time(),
                source="ws:bybit.orderbook1",
            )
        )

    @staticmethod
    def _to_float(value) -> Optional[float]:
        try:
            out = float(value)
            return out if out > 0 else None
        except (TypeError, ValueError):
            return None
