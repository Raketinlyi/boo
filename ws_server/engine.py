from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

import aiohttp
from aiohttp_retry import ExponentialRetry, RetryClient

from arbitrage_logic.calculator import ArbitrageCalculator
from blacklist import BlacklistManager
from config import Config
from ws_server.adapters.bitget import BitgetTickerAdapter
from ws_server.adapters.bybit import BybitTickerAdapter
from ws_server.adapters.coinex import CoinExBboAdapter
from ws_server.adapters.binanceus import BinanceUsBookTickerAdapter
from ws_server.adapters.krakenpro import KrakenProBookAdapter
from ws_server.adapters.pionexus import PionexUsDepthAdapter
from ws_server.adapters.lbank import LBankDepthAdapter
from ws_server.adapters.gateio import GateIoBookTickerAdapter
from ws_server.adapters.kucoin import KuCoinTickerAdapter
from ws_server.adapters.mexc import MexcBookTickerAdapter
from ws_server.adapters.okx import OkxTickerAdapter
from ws_server.models import Quote
from ws_server.store import QuoteStore


class WsArbitrageEngine:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = QuoteStore()
        self.calc = ArbitrageCalculator(config)
        self.tasks: List[asyncio.Task] = []
        self.running = False
        self.last_rest_poll_ts = 0.0
        self.last_pairs_update_ts = 0.0
        self.common_pairs_count = 0
        self.enabled_exchanges: List[str] = []

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        await self._update_pairs()
        self.tasks.append(asyncio.create_task(self._rest_fallback_loop(), name="ws-rest-fallback"))
        self.tasks.append(asyncio.create_task(self._adapter_loop(GateIoBookTickerAdapter, "Gate.io", "ws_gate_max_symbols"), name="ws-gateio"))
        self.tasks.append(asyncio.create_task(self._adapter_loop(KuCoinTickerAdapter, "KuCoin", "ws_kucoin_max_symbols"), name="ws-kucoin"))
        self.tasks.append(asyncio.create_task(self._adapter_loop(OkxTickerAdapter, "OKX", "ws_okx_max_symbols"), name="ws-okx"))
        # Bybit shards across multiple WS connections (v5 public spot limits
        # topics per connection, especially for orderbook.1). Split the full
        # symbol list into chunks of ws_bybit_shard_size and open up to
        # ws_bybit_max_connections parallel sockets.
        self._start_bybit_shards()
        self._start_mexc_shards()
        self.tasks.append(asyncio.create_task(self._adapter_loop(BitgetTickerAdapter, "Bitget", "ws_bitget_max_symbols"), name="ws-bitget"))
        self.tasks.append(asyncio.create_task(self._adapter_loop(CoinExBboAdapter, "CoinEx", "ws_coinex_max_symbols"), name="ws-coinex"))
        self.tasks.append(asyncio.create_task(self._adapter_loop(BinanceUsBookTickerAdapter, "Binance.US", "ws_binanceus_max_symbols"), name="ws-binanceus"))
        self.tasks.append(asyncio.create_task(self._adapter_loop(KrakenProBookAdapter, "Kraken Pro", "ws_krakenpro_max_symbols"), name="ws-krakenpro"))
        self.tasks.append(asyncio.create_task(self._adapter_loop(PionexUsDepthAdapter, "Pionex.US", "ws_pionexus_max_symbols"), name="ws-pionexus"))
        self.tasks.append(asyncio.create_task(self._adapter_loop(LBankDepthAdapter, "LBank", "ws_lbank_max_symbols"), name="ws-lbank"))
    async def stop(self) -> None:
        self.running = False
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def _update_pairs(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        retry_options = ExponentialRetry(attempts=3, start_timeout=1, max_timeout=8, statuses={429, 500, 502, 503, 504})
        try:
            manager = BlacklistManager()
            self.calc.set_permanent_blacklist(manager.permanent_blacklist)
        except Exception as exc:
            logging.warning("WS server failed to load permanent blacklist: %s", exc)
        async with RetryClient(aiohttp.ClientSession(timeout=timeout), retry_options=retry_options) as session:
            await self.calc.update_common_pairs(session)
        self.common_pairs_count = len(self.calc.common_pairs)
        self.enabled_exchanges = [ex.name for ex in self.calc.get_enabled_exchanges()]
        self.last_pairs_update_ts = time.time()
        logging.info("WS server pairs ready: %s common pairs, exchanges=%s", self.common_pairs_count, self.enabled_exchanges)

    async def _rest_fallback_loop(self) -> None:
        while self.running:
            try:
                if time.time() - self.last_pairs_update_ts > self._pairs_interval_sec():
                    await self._update_pairs()

                timeout = aiohttp.ClientTimeout(total=30)
                retry_options = ExponentialRetry(attempts=3, start_timeout=1, max_timeout=8, statuses={429, 500, 502, 503, 504})
                async with RetryClient(aiohttp.ClientSession(timeout=timeout), retry_options=retry_options) as session:
                    all_prices = await self.calc.fetch_batch_prices(session)

                quotes = []
                now = time.time()
                for exchange, by_symbol in (all_prices or {}).items():
                    if not isinstance(by_symbol, dict):
                        continue
                    for symbol, price in by_symbol.items():
                        try:
                            p = float(price)
                        except (TypeError, ValueError):
                            continue
                        if p <= 0:
                            continue
                        quotes.append(
                            Quote(
                                exchange=str(exchange),
                                symbol=str(symbol).strip().upper(),
                                bid=None,
                                ask=None,
                                last=p,
                                ts=now,
                                source="rest:fallback",
                            )
                        )
                await self.store.upsert_many(quotes)
                self.last_rest_poll_ts = now
                logging.info("WS server REST fallback stored %s quotes", len(quotes))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.warning("WS server REST fallback failed: %s", exc)

            await asyncio.sleep(self._rest_interval_sec())

    async def _adapter_loop(self, adapter_cls, exchange_name: str, max_symbols_key: str) -> None:
        startup_delay = self._adapter_startup_delay(exchange_name)
        if startup_delay > 0:
            await asyncio.sleep(startup_delay)
        while self.running:
            symbols = self._symbols_for_exchange(exchange_name, max_symbols_key)
            adapter = adapter_cls(self.store, symbols)
            try:
                await adapter.run()
            except asyncio.CancelledError:
                await adapter.stop()
                raise
            except Exception as exc:
                logging.warning("%s WS adapter failed: %s", exchange_name, exc)
            await asyncio.sleep(self._adapter_reconnect_delay())

    async def _adapter_loop_fixed_symbols(
        self, adapter_cls, display_name: str, symbols: List[str]
    ) -> None:
        """Run a WS adapter on a pre-computed symbol list (used for sharded connections)."""
        startup_delay = self._adapter_startup_delay(display_name)
        if startup_delay > 0:
            await asyncio.sleep(startup_delay)
        while self.running:
            adapter = adapter_cls(self.store, list(symbols))
            try:
                await adapter.run()
            except asyncio.CancelledError:
                await adapter.stop()
                raise
            except Exception as exc:
                logging.warning("%s WS adapter failed: %s", display_name, exc)
            await asyncio.sleep(self._adapter_reconnect_delay())

    def _start_bybit_shards(self) -> None:
        """Spawn multiple Bybit WS connections, each subscribed to a symbol shard."""
        try:
            shard_size = max(5, int(self.config.get("ws_bybit_shard_size", 30) or 30))
        except Exception:
            shard_size = 30
        try:
            max_conns = max(1, int(self.config.get("ws_bybit_max_connections", 8) or 8))
        except Exception:
            max_conns = 8
        total_cap = shard_size * max_conns
        symbols = self._symbols_for_exchange(
            "Bybit", "ws_bybit_max_symbols", bybit_hard_cap=total_cap
        )
        if not symbols:
            logging.warning("Bybit WS: no symbols available for sharded subscription")
            return
        shard_count = 0
        for start in range(0, len(symbols), shard_size):
            shard = symbols[start : start + shard_size]
            if not shard:
                break
            shard_count += 1
            display_name = f"Bybit#{shard_count}"
            self.tasks.append(
                asyncio.create_task(
                    self._adapter_loop_fixed_symbols(
                        BybitTickerAdapter, display_name, shard
                    ),
                    name=f"ws-bybit-shard-{shard_count}",
                )
            )
            if shard_count >= max_conns:
                break
        logging.info(
            "Bybit WS sharded: %s connections × up to %s symbols (total=%s)",
            shard_count,
            shard_size,
            min(len(symbols), shard_count * shard_size),
        )

    def _symbols_for_exchange(
        self,
        exchange_name: str,
        max_symbols_key: str,
        *,
        bybit_hard_cap: Optional[int] = None,
    ) -> List[str]:
        # ВАЖНО: если биржа отключена в config.enabled_exchanges,
        # WS-адаптер не должен подписываться на общие пары других бирж.
        # В v1 здесь была ошибка: disabled exchange с пустым available_pairs
        # получала sorted(common) и всё равно пыталась подключаться.
        exchange_obj = None
        for ex in getattr(self.calc, "exchanges", []) or []:
            if str(getattr(ex, "name", "")).lower() == exchange_name.lower():
                exchange_obj = ex
                break
        if exchange_obj is None or not bool(getattr(exchange_obj, "enabled", False)):
            return []

        try:
            max_symbols = int(self.config.get(max_symbols_key, 3000) or 3000)
        except Exception:
            max_symbols = 3000
        if exchange_name.lower() == "bybit" and max_symbols_key == "ws_bybit_max_symbols":
            # When sharding, allow up to bybit_hard_cap (shard_size * max_conns).
            # Otherwise fall back to the legacy 250-symbol single-connection cap.
            effective_cap = int(bybit_hard_cap) if bybit_hard_cap else 250
            max_symbols = min(max_symbols, effective_cap)
        max_symbols = max(1, min(3000, max_symbols))
        available = getattr(exchange_obj, "available_pairs", None)
        common = set(self.calc.common_pairs or [])
        if isinstance(available, set) and available:
            symbols = sorted(common.intersection(available))
        else:
            symbols = sorted(common)
        if exchange_name.lower() == "bybit":
            priority = [
                "BTCUSDT",
                "ETHUSDT",
                "SOLUSDT",
                "XRPUSDT",
                "BNBUSDT",
                "DOGEUSDT",
                "ADAUSDT",
                "TRXUSDT",
                "LINKUSDT",
                "TONUSDT",
                "AVAXUSDT",
                "SUIUSDT",
                "LTCUSDT",
                "BCHUSDT",
                "DOTUSDT",
                "UNIUSDT",
                "AAVEUSDT",
                "APTUSDT",
                "ARBUSDT",
                "OPUSDT",
                "WIFUSDT",
                "PEPEUSDT",
                "ENAUSDT",
                "NEARUSDT",
                "ATOMUSDT",
                "ETCUSDT",
                "FILUSDT",
                "INJUSDT",
                "MKRUSDT",
                "SEIUSDT",
            ]
            symbol_set = set(symbols)
            ordered = [sym for sym in priority if sym in symbol_set]
            ordered.extend(sym for sym in symbols if sym not in set(ordered))
            symbols = ordered
        return symbols[:max_symbols]

    def _start_mexc_shards(self) -> None:
        """Spawn MEXC protobuf bookTicker WS connections.

        MEXC allows only 30 subscriptions per connection, so this mirrors the
        Bybit shard pattern with a MEXC-specific hard cap.
        """
        try:
            shard_size = max(1, min(30, int(self.config.get("ws_mexc_shard_size", 30) or 30)))
        except Exception:
            shard_size = 30
        try:
            max_conns = max(1, int(self.config.get("ws_mexc_max_connections", 10) or 10))
        except Exception:
            max_conns = 10
        total_cap = shard_size * max_conns
        symbols = self._symbols_for_exchange("MEXC", "ws_mexc_max_symbols")[:total_cap]
        if not symbols:
            logging.warning("MEXC WS: no symbols available for sharded subscription")
            return
        shard_count = 0
        for start in range(0, len(symbols), shard_size):
            shard = symbols[start : start + shard_size]
            if not shard:
                break
            shard_count += 1
            display_name = f"MEXC#{shard_count}"
            self.tasks.append(
                asyncio.create_task(
                    self._adapter_loop_fixed_symbols(
                        MexcBookTickerAdapter, display_name, shard
                    ),
                    name=f"ws-mexc-shard-{shard_count}",
                )
            )
            if shard_count >= max_conns:
                break
        logging.info(
            "MEXC WS sharded: %s connections x up to %s symbols (total=%s)",
            shard_count,
            shard_size,
            min(len(symbols), shard_count * shard_size),
        )

    def _rest_interval_sec(self) -> float:
        try:
            return max(5.0, float(self.config.get("ws_rest_fallback_interval_sec", 30) or 30))
        except Exception:
            return 30.0

    def _pairs_interval_sec(self) -> float:
        try:
            return max(300.0, float(self.config.get("pairs_update_interval", 3) or 3) * 60.0)
        except Exception:
            return 1800.0

    async def health(self) -> dict:
        snap = await self.store.snapshot()
        ws_quotes = 0
        rest_quotes = 0
        ws_by_exchange: Dict[str, int] = {}
        for by_symbol in snap.values():
            for quote in by_symbol.values():
                if quote.source.startswith("ws:"):
                    ws_quotes += 1
                    ws_by_exchange[quote.exchange] = ws_by_exchange.get(quote.exchange, 0) + 1
                elif quote.source.startswith("rest:"):
                    rest_quotes += 1
        return {
            "success": True,
            "running": self.running,
            "uptime_sec": int(time.time() - self.store.started_at),
            "common_pairs": self.common_pairs_count,
            "enabled_exchanges": self.enabled_exchanges,
            "quotes_by_exchange": {ex: len(rows) for ex, rows in snap.items()},
            "ws_quotes_by_exchange": ws_by_exchange,
            "ws_quotes": ws_quotes,
            "rest_quotes": rest_quotes,
            "last_rest_poll_age_sec": int(time.time() - self.last_rest_poll_ts) if self.last_rest_poll_ts else None,
        }
