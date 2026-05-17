from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import WSMsgType, web

from config import Config
from ws_server.engine import WsArbitrageEngine


def create_app(config: Config) -> web.Application:
    engine = WsArbitrageEngine(config)
    app = web.Application()
    app["engine"] = engine

    async def on_startup(_: web.Application) -> None:
        await engine.start()

    async def on_cleanup(_: web.Application) -> None:
        await engine.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    async def health(_: web.Request) -> web.Response:
        return web.json_response(await engine.health())

    async def quotes(request: web.Request) -> web.Response:
        snap = await engine.store.snapshot()
        data = {
            exchange: {symbol: quote.to_dict() for symbol, quote in by_symbol.items()}
            for exchange, by_symbol in snap.items()
        }
        return _json_with_cors({"success": True, "data": data})

    async def symbol_quotes(request: web.Request) -> web.Response:
        symbol = request.match_info.get("symbol", "").strip().upper()
        rows = await engine.store.symbol_quotes(symbol)
        return _json_with_cors(
            {
                "success": True,
                "symbol": symbol,
                "data": {exchange: quote.to_dict() for exchange, quote in rows.items()},
            }
        )

    async def opportunities(request: web.Request) -> web.Response:
        min_spread = _float_arg(request, "min_spread", float(config.get("min_spread", 0.5) or 0.5))
        max_spread = _float_arg(request, "max_spread", float(config.get("max_spread", 77.0) or 77.0))
        ttl_sec = _float_arg(request, "ttl_sec", float(config.get("ws_quote_ttl_sec", 10.0) or 10.0))
        limit = int(_float_arg(request, "limit", 80))
        notional_usd = _float_arg(request, "notional_usd", float(config.get("arb_min_notional_usd", 0.0) or 0.0))
        require_top_liquidity = _bool_arg(
            request,
            "require_top_liquidity",
            bool(config.get("ws_require_top_liquidity", True)),
        )
        rows = await engine.store.build_opportunities(
            min_spread=min_spread,
            max_spread=max_spread,
            ttl_sec=ttl_sec,
            limit=limit,
            notional_usd=notional_usd,
            require_top_liquidity=require_top_liquidity,
        )
        return _json_with_cors({"success": True, "data": [row.to_dict() for row in rows]})

    async def ws_stream(request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint for real-time quotes and opportunities.

        The browser connects once and receives periodic `snapshot` messages
        (every ~1s) containing fresh quotes and top opportunities. This
        replaces the 7-second polling of /api/opportunities on the Flask
        side, giving the UI true real-time updates.

        Message format (server → client):
            {"type":"snapshot","ts":...,"opportunities":[...],
             "quotes":{exchange:{symbol:{bid,ask,...}}}}

        The client can also send:
            {"type":"subscribe","symbols":["BTCUSDT","ETHUSDT"]}
        to receive a restricted subset (saves bandwidth for weak clients).
        """
        ws = web.WebSocketResponse(heartbeat=30, compress=True)
        await ws.prepare(request)

        # Per-connection state.
        subscribed_symbols: set[str] | None = None  # None = all
        try:
            interval_sec = max(
                0.5, float(config.get("ws_browser_push_interval_sec", 1.0) or 1.0)
            )
        except Exception:
            interval_sec = 1.0
        try:
            min_spread = float(config.get("min_spread", 0.5) or 0.5)
            max_spread = float(config.get("max_spread", 77.0) or 77.0)
            ttl_sec = float(config.get("ws_quote_ttl_sec", 10.0) or 10.0)
            limit = int(config.get("ws_browser_opp_limit", 80) or 80)
        except Exception:
            min_spread, max_spread, ttl_sec, limit = 0.5, 77.0, 10.0, 80

        client_id = request.remote or "unknown"
        logging.info("[WS] client connected from %s", client_id)

        async def reader() -> None:
            nonlocal subscribed_symbols
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except Exception:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    mtype = str(payload.get("type") or "").lower()
                    if mtype == "subscribe":
                        symbols = payload.get("symbols")
                        if isinstance(symbols, list):
                            subscribed_symbols = {
                                str(s).strip().upper() for s in symbols if s
                            } or None
                            logging.info(
                                "[WS] %s subscribed to %s symbols",
                                client_id,
                                len(subscribed_symbols) if subscribed_symbols else "ALL",
                            )
                    elif mtype == "unsubscribe":
                        subscribed_symbols = None
                    elif mtype == "ping":
                        await ws.send_json({"type": "pong", "ts": time.time()})
                elif msg.type == WSMsgType.ERROR:
                    logging.warning("[WS] client %s error: %s", client_id, ws.exception())
                    break

        async def pusher() -> None:
            # Send initial snapshot immediately so the UI renders without delay.
            try:
                await ws.send_json(
                    await _build_snapshot(
                        engine,
                        min_spread=min_spread,
                        max_spread=max_spread,
                        ttl_sec=ttl_sec,
                        limit=limit,
                        subscribed=subscribed_symbols,
                    )
                )
            except ConnectionResetError:
                return
            except Exception as exc:
                logging.debug("[WS] initial snapshot failed: %s", exc)

            while not ws.closed:
                await asyncio.sleep(interval_sec)
                if ws.closed:
                    break
                try:
                    snap = await _build_snapshot(
                        engine,
                        min_spread=min_spread,
                        max_spread=max_spread,
                        ttl_sec=ttl_sec,
                        limit=limit,
                        subscribed=subscribed_symbols,
                    )
                    await ws.send_json(snap)
                except ConnectionResetError:
                    break
                except Exception as exc:
                    logging.debug("[WS] push failed: %s", exc)
                    break

        try:
            await asyncio.gather(reader(), pusher())
        finally:
            logging.info("[WS] client %s disconnected", client_id)
        return ws

    app.router.add_get("/health", health)
    app.router.add_get("/quotes", quotes)
    app.router.add_get("/quotes/{symbol}", symbol_quotes)
    app.router.add_get("/opportunities", opportunities)
    app.router.add_get("/ws", ws_stream)
    app.router.add_get("/ws/quotes", ws_stream)

    # CORS preflight helper (browser connects from http://127.0.0.1:8080 → 8090).
    async def cors_preflight(request: web.Request) -> web.Response:
        return _cors_response()
    app.router.add_options("/{path:.*}", cors_preflight)

    return app


async def _build_snapshot(
    engine: WsArbitrageEngine,
    *,
    min_spread: float,
    max_spread: float,
    ttl_sec: float,
    limit: int,
    subscribed: set | None,
) -> dict:
    """Build one WS payload: top opportunities + quotes for active symbols."""
    rows = await engine.store.build_opportunities(
        min_spread=min_spread,
        max_spread=max_spread,
        ttl_sec=ttl_sec,
        limit=limit,
        notional_usd=0.0,
        require_top_liquidity=False,
    )
    opps = [row.to_dict() for row in rows]

    # Only ship quotes for symbols the client cares about (subscribed) OR for
    # the top-N opportunities (so the UI always has fresh prices for visible
    # rows). Full snapshots would be too large.
    relevant_symbols: set = set()
    if subscribed:
        relevant_symbols = subscribed
    else:
        relevant_symbols = {row.get("symbol") for row in opps if row.get("symbol")}

    quotes_out: dict = {}
    if relevant_symbols:
        snap = await engine.store.snapshot()
        for exchange, by_symbol in snap.items():
            for symbol, quote in by_symbol.items():
                if symbol in relevant_symbols:
                    quotes_out.setdefault(exchange, {})[symbol] = quote.to_dict()

    return {
        "type": "snapshot",
        "ts": time.time(),
        "opportunities": opps,
        "quotes": quotes_out,
    }


def _json_with_cors(payload: dict) -> web.Response:
    resp = web.json_response(payload)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    return resp


def _cors_response() -> web.Response:
    resp = web.Response(status=204)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    return resp


def _float_arg(request: web.Request, name: str, default: float) -> float:
    try:
        return float(request.query.get(name, default))
    except Exception:
        return default


def _bool_arg(request: web.Request, name: str, default: bool) -> bool:
    raw = request.query.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}
