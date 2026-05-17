from __future__ import annotations

import gzip
import json
import logging
import time
from typing import Iterable, Optional

import aiohttp

from utils.symbols import normalize_pair_symbol
from ws_server.adapters.base import WsAdapter
from ws_server.models import Quote
from ws_server.store import QuoteStore


class CoinExBboAdapter(WsAdapter):
    """CoinEx spot BBO listener.

    Official WS endpoint: wss://socket.coinex.com/v2/spot
    Method: bbo.subscribe. CoinEx sends gzip-compressed binary frames.
    """

    name = "CoinEx"
    url = "wss://socket.coinex.com/v2/spot"

    def __init__(self, store: QuoteStore, symbols: Iterable[str], *, batch_size: int = 80) -> None:
        super().__init__(store, symbols)
        self.batch_size = max(1, int(batch_size or 80))

    async def run(self) -> None:
        if not self.symbols:
            logging.warning("CoinEx WS: no symbols to subscribe")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url, heartbeat=20, receive_timeout=60) as ws:
                        await self._subscribe(ws)
                        logging.info("CoinEx WS connected (%s symbols)", len(self.symbols))
                        backoff = 1.0
                        async for msg in ws:
                            if self._stop.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_payload(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                await self._handle_payload(self._decode_binary(msg.data))
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except Exception as exc:
                self.log_exception("connection dropped", exc)
            if await self.sleep_or_stop(backoff):
                break
            backoff = min(30.0, backoff * 2.0)

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        for idx, start in enumerate(range(0, len(self.symbols), self.batch_size), start=1):
            chunk = self.symbols[start : start + self.batch_size]
            await ws.send_json({"method": "bbo.subscribe", "params": {"market_list": chunk}, "id": idx})

    async def _handle_payload(self, raw) -> None:
        if not raw:
            return
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("method") != "bbo.update":
            return
        data = msg.get("data")
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = normalize_pair_symbol(row.get("market"))
            bid = self._to_float(row.get("best_bid_price"))
            ask = self._to_float(row.get("best_ask_price"))
            if not symbol or not bid or not ask:
                continue
            ts_raw = self._to_float(row.get("updated_at"))
            ts = (ts_raw / 1000.0) if ts_raw and ts_raw > 10_000_000_000 else time.time()
            await self.store.upsert(
                Quote(
                    exchange=self.name,
                    symbol=symbol,
                    bid=bid,
                    ask=ask,
                    bid_size=self._to_float(row.get("best_bid_size")),
                    ask_size=self._to_float(row.get("best_ask_size")),
                    last=None,
                    ts=ts,
                    source="ws:coinex.bbo",
                )
            )

    @staticmethod
    def _decode_binary(data: bytes) -> str:
        try:
            return gzip.decompress(data).decode("utf-8")
        except Exception:
            try:
                return data.decode("utf-8")
            except Exception:
                return ""

    @staticmethod
    def _to_float(value) -> Optional[float]:
        try:
            out = float(value)
            return out if out > 0 else None
        except (TypeError, ValueError):
            return None
