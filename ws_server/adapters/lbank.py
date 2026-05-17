from __future__ import annotations

import json
import logging
import time
from typing import Optional

import aiohttp

from utils.symbols import normalize_pair_symbol, split_pair_symbol
from ws_server.adapters.base import WsAdapter
from ws_server.models import Quote


class LBankDepthAdapter(WsAdapter):
    name = "LBank"
    url = "wss://www.lbkex.net/ws/V2/"

    async def run(self) -> None:
        if not self.symbols:
            logging.warning("LBank WS: no symbols to subscribe")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url, heartbeat=20, receive_timeout=60) as ws:
                        await self._subscribe(ws)
                        logging.info("LBank WS connected (%s symbols)", len(self.symbols[:200]))
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

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # Keep LBank conservative; REST fallback still covers all symbols.
        for symbol in self.symbols[:200]:
            base, quote = split_pair_symbol(symbol)
            if base and quote:
                await ws.send_json({"action": "subscribe", "subscribe": "depth", "depth": "10", "pair": f"{base.lower()}_{quote.lower()}"})
                await self.sleep_or_stop(0.02)

    async def _handle_message(self, ws: aiohttp.ClientWebSocketResponse, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if str(msg.get("action") or "").lower() == "ping" and msg.get("ping"):
            try:
                await ws.send_json({"action": "pong", "pong": msg.get("ping")})
            except Exception:
                pass
            return
        if msg.get("type") != "depth" and "depth" not in msg:
            return
        raw_symbol = str(msg.get("pair") or "").upper().replace("-", "_").replace("/", "_")
        payload = msg.get("depth") if isinstance(msg.get("depth"), dict) else msg
        bids = payload.get("bids") if isinstance(payload, dict) else None
        asks = payload.get("asks") if isinstance(payload, dict) else None
        if not raw_symbol or not bids or not asks:
            return
        bid = self._px(bids[0]); ask = self._px(asks[0])
        bid_sz = self._sz(bids[0]); ask_sz = self._sz(asks[0])
        if not bid or not ask:
            return
        await self.store.upsert(Quote(exchange=self.name, symbol=normalize_pair_symbol(raw_symbol), bid=bid, ask=ask,
                                      bid_size=bid_sz, ask_size=ask_sz, ts=time.time(), source="ws:lbank.depth"))

    @staticmethod
    def _px(row) -> Optional[float]:
        try:
            v = row[0] if isinstance(row, (list, tuple)) else row.get("price")
            out = float(v)
            return out if out > 0 else None
        except Exception:
            return None

    @staticmethod
    def _sz(row) -> Optional[float]:
        try:
            v = row[1] if isinstance(row, (list, tuple)) else row.get("qty") or row.get("quantity") or row.get("size")
            out = float(v)
            return out if out > 0 else None
        except Exception:
            return None
