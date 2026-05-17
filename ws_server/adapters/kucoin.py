from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Iterable, Optional

import aiohttp

from utils.symbols import format_pair_symbol, normalize_pair_symbol
from ws_server.adapters.base import WsAdapter
from ws_server.models import Quote
from ws_server.store import QuoteStore


class KuCoinTickerAdapter(WsAdapter):
    name = "KuCoin"
    bullet_url = "https://api.kucoin.com/api/v1/bullet-public"

    def __init__(self, store: QuoteStore, symbols: Iterable[str], *, batch_size: int = 80) -> None:
        super().__init__(store, symbols)
        self.batch_size = max(1, int(batch_size or 80))

    async def run(self) -> None:
        if not self.symbols:
            logging.warning("KuCoin WS: no symbols to subscribe")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    endpoint, token = await self._get_endpoint(session)
                    connect_id = uuid.uuid4().hex
                    url = f"{endpoint}?token={token}&connectId={connect_id}"
                    async with session.ws_connect(url, heartbeat=20, receive_timeout=60) as ws:
                        await self._subscribe(ws)
                        logging.info("KuCoin WS connected (%s symbols)", len(self.symbols))
                        backoff = 1.0
                        async for msg in ws:
                            if self._stop.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(ws, msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except Exception as exc:
                self.log_exception("connection dropped", exc)
            if await self.sleep_or_stop(backoff):
                break
            backoff = min(30.0, backoff * 2.0)

    async def _get_endpoint(self, session: aiohttp.ClientSession) -> tuple[str, str]:
        async with session.post(self.bullet_url) as resp:
            data = await resp.json(content_type=None)
        token = ((data.get("data") or {}).get("token") or "").strip()
        servers = (data.get("data") or {}).get("instanceServers") or []
        if not token or not servers:
            raise RuntimeError("KuCoin bullet-public did not return token/endpoint")
        endpoint = str(servers[0].get("endpoint") or "").strip()
        if not endpoint:
            raise RuntimeError("KuCoin bullet-public endpoint is empty")
        return endpoint, token

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        raw_symbols = [format_pair_symbol(sym, sep="-") for sym in self.symbols]
        for i in range(0, len(raw_symbols), self.batch_size):
            topic = "/market/ticker:" + ",".join(raw_symbols[i : i + self.batch_size])
            await ws.send_json(
                {
                    "id": str(int(time.time() * 1000)),
                    "type": "subscribe",
                    "topic": topic,
                    "privateChannel": False,
                    "response": True,
                }
            )

    async def _handle_message(self, ws: aiohttp.ClientWebSocketResponse, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("type") == "ping":
            await ws.send_json({"id": msg.get("id") or str(int(time.time() * 1000)), "type": "pong"})
            return
        if msg.get("type") != "message":
            return
        topic = str(msg.get("topic") or "")
        row = msg.get("data")
        if not isinstance(row, dict):
            return
        raw_symbol = row.get("symbol") or topic.rsplit(":", 1)[-1]
        symbol = normalize_pair_symbol(raw_symbol)
        bid = self._to_float(row.get("bestBid") or row.get("bestBidPrice"))
        ask = self._to_float(row.get("bestAsk") or row.get("bestAskPrice"))
        last = self._to_float(row.get("price") or row.get("last"))
        if not symbol or not (bid or ask or last):
            return
        await self.store.upsert(
            Quote(
                exchange=self.name,
                symbol=symbol,
                bid=bid,
                ask=ask,
                bid_size=self._to_float(row.get("bestBidSize")),
                ask_size=self._to_float(row.get("bestAskSize")),
                last=last,
                ts=time.time(),
                source="ws:kucoin.ticker",
            )
        )

    @staticmethod
    def _to_float(value) -> Optional[float]:
        try:
            out = float(value)
            return out if out > 0 else None
        except (TypeError, ValueError):
            return None
