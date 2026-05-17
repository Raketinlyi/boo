from __future__ import annotations

import json
import logging
import time
from typing import Dict, Iterable, Optional, Tuple

import aiohttp

from utils.symbols import normalize_pair_symbol
from ws_server.adapters.base import WsAdapter
from ws_server.models import Quote
from ws_server.store import QuoteStore


class MexcBookTickerAdapter(WsAdapter):
    """MEXC spot book-ticker listener.

    MEXC Spot v3 market WebSocket currently pushes protobuf frames. This
    adapter decodes only the wrapper and bookTicker fields we need, avoiding a
    generated-protobuf dependency in the trading image.
    """

    name = "MEXC"
    url = "wss://wbs-api.mexc.com/ws"

    def __init__(self, store: QuoteStore, symbols: Iterable[str]) -> None:
        super().__init__(store, symbols)
        # MEXC docs limit one connection to 30 subscriptions.
        self.symbols = self.symbols[:30]

    async def run(self) -> None:
        if not self.symbols:
            logging.warning("MEXC WS: no symbols to subscribe")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url, heartbeat=20, receive_timeout=60) as ws:
                        await self._subscribe(ws)
                        logging.info("MEXC WS connected (%s symbols)", len(self.symbols))
                        backoff = 1.0
                        async for msg in ws:
                            if self._stop.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                await self._handle_binary(msg.data)
                            elif msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_text(ws, msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except Exception as exc:
                self.log_exception("connection dropped", exc)
            if await self.sleep_or_stop(backoff):
                break
            backoff = min(30.0, backoff * 2.0)

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        params = [f"spot@public.aggre.bookTicker.v3.api.pb@100ms@{sym}" for sym in self.symbols]
        await ws.send_json({"method": "SUBSCRIPTION", "params": params})

    async def _handle_text(self, ws: aiohttp.ClientWebSocketResponse, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("msg") == "PING":
            await ws.send_json({"method": "PING"})
            return
        code = msg.get("code")
        text = str(msg.get("msg") or "")
        if code not in (None, 0, "0") and text:
            logging.warning("MEXC WS subscription/control response: code=%s msg=%s", code, text)

    async def _handle_binary(self, data: bytes) -> None:
        wrapper = self._parse_wrapper(data)
        symbol = normalize_pair_symbol(wrapper.get("symbol"))
        ticker = wrapper.get("ticker") or {}
        if not symbol or not ticker:
            return
        bid = self._to_float(ticker.get("bidPrice"))
        ask = self._to_float(ticker.get("askPrice"))
        if not bid or not ask:
            return
        ts_raw = self._to_float(wrapper.get("sendTime") or wrapper.get("createTime"))
        ts = (ts_raw / 1000.0) if ts_raw and ts_raw > 10_000_000_000 else time.time()
        await self.store.upsert(
            Quote(
                exchange=self.name,
                symbol=symbol,
                bid=bid,
                ask=ask,
                bid_size=self._to_float(ticker.get("bidQuantity")),
                ask_size=self._to_float(ticker.get("askQuantity")),
                last=None,
                ts=ts,
                source="ws:mexc.book_ticker",
            )
        )

    @classmethod
    def _parse_wrapper(cls, data: bytes) -> Dict[str, object]:
        out: Dict[str, object] = {}
        pos = 0
        while pos < len(data):
            field, wire, pos = cls._read_key(data, pos)
            if wire == 2:
                raw, pos = cls._read_bytes(data, pos)
                if field == 1:
                    out["channel"] = cls._decode_text(raw)
                elif field == 3:
                    out["symbol"] = cls._decode_text(raw)
                elif field in (305, 315):
                    out["ticker"] = cls._parse_ticker(raw)
            elif wire == 0:
                value, pos = cls._read_varint(data, pos)
                if field == 5:
                    out["createTime"] = value
                elif field == 6:
                    out["sendTime"] = value
            else:
                pos = cls._skip_unknown(data, pos, wire)
        return out

    @classmethod
    def _parse_ticker(cls, data: bytes) -> Dict[str, str]:
        names = {1: "bidPrice", 2: "bidQuantity", 3: "askPrice", 4: "askQuantity"}
        out: Dict[str, str] = {}
        pos = 0
        while pos < len(data):
            field, wire, pos = cls._read_key(data, pos)
            if wire == 2:
                raw, pos = cls._read_bytes(data, pos)
                if field in names:
                    out[names[field]] = cls._decode_text(raw)
            elif wire == 0:
                _, pos = cls._read_varint(data, pos)
            else:
                pos = cls._skip_unknown(data, pos, wire)
        return out

    @classmethod
    def _read_key(cls, data: bytes, pos: int) -> Tuple[int, int, int]:
        key, pos = cls._read_varint(data, pos)
        return key >> 3, key & 7, pos

    @staticmethod
    def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
        shift = 0
        value = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value, pos
            shift += 7
            if shift > 70:
                break
        raise ValueError("invalid protobuf varint")

    @classmethod
    def _read_bytes(cls, data: bytes, pos: int) -> Tuple[bytes, int]:
        length, pos = cls._read_varint(data, pos)
        end = pos + length
        if end > len(data):
            raise ValueError("invalid protobuf length")
        return data[pos:end], end

    @classmethod
    def _skip_unknown(cls, data: bytes, pos: int, wire: int) -> int:
        if wire == 1:
            return min(len(data), pos + 8)
        if wire == 5:
            return min(len(data), pos + 4)
        if wire == 2:
            _, pos = cls._read_bytes(data, pos)
            return pos
        raise ValueError(f"unsupported protobuf wire type {wire}")

    @staticmethod
    def _decode_text(data: bytes) -> str:
        return data.decode("utf-8", errors="ignore")

    @staticmethod
    def _to_float(value) -> Optional[float]:
        try:
            out = float(value)
            return out if out > 0 else None
        except (TypeError, ValueError):
            return None
