"""
Module for running the bot's web interface.

This module creates a simple Flask web server that:
1. Displays current arbitrage opportunities
2. Allows bot control through web interface
3. Provides data access API
"""
import threading
import logging
import json
import webbrowser
import os
import re
import time
import socket
import asyncio
import aiohttp
import urllib.parse
import urllib.request
from typing import Dict, List, Any, Optional, Tuple
from collections import OrderedDict, deque
import traceback
from datetime import datetime
# from arbitrage_logic.websocket_arbitrage import WebSocketArbitrageTracker  # Removed (transitioning to real trading)

from utils.symbols import extract_base_asset, split_pair_symbol
from utils.bridge_providers import get_bridge_candidates
from utils.exchange_info import exchange_info_fetcher, get_coin_info
from utils.interchain_live_quotes import (
    build_jupiter_dex_quote,
    canon_chain_name,
    fetch_debridge_quote,
    fetch_debridge_rebalance_quote,
    fetch_geckoterminal_dex_quotes,
    fetch_layerzero_quote,
    fetch_layerzero_rebalance_quote,
    fetch_mayan_quote,
    fetch_mayan_rebalance_quote,
    fetch_relay_chains,
    fetch_relay_quote,
    fetch_relay_rebalance_quote,
    fetch_wormhole_quote,
    fetch_wormhole_rebalance_quote,
    discover_symbol_contracts,
    get_mayan_supported_token,
    get_relay_featured_token,
    resolve_mayan_asset_tokens,
    resolve_solana_token,
)
from web_interface.kraken_kyber_scanner import KrakenKyberScanner, get_kraken_kyber_index_status
from web_interface.contract_first_interchain import (
    CONTRACT_FIRST_PRIORITY_ASSETS,
    STABLE_ASSETS,
    handle_contract_first_interchain_debug,
    handle_contract_first_interchain_opportunities,
)

# Import Flask and necessary components
try:
    from flask import Flask, render_template, jsonify, request, Response, abort, send_from_directory
    from flask_cors import CORS
    app = Flask(__name__, template_folder='templates', static_folder='static')
    CORS(app)  # Allow cross-domain requests
except ImportError:
    logging.error("Flask is not installed. Web interface will not be available.")
    logging.error("Install Flask: pip install flask flask-cors")
    raise

# Replace any imports from gui with our StatusVar class
class StatusVar:
    def __init__(self, value=""):
        self.value = value
    
    def set(self, value):
        self.value = value
        logging.info(f"Status: {value}")
    
    def get(self):
        return self.value

# Global variables
server_thread = None
is_running = False
bot_instance = None
port = 8080

# Vite build output (optional)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VITE_DIST_DIR = os.path.join(BASE_DIR, 'dist')
VITE_ASSETS_DIR = os.path.join(VITE_DIST_DIR, 'assets')

# Data for display
current_data = {
    "opportunities": [],
    "status": "Stopped",
    "last_update": "",
    "common_pairs": 0,
    "enabled_exchanges": []
}

# CoinGecko data cache
coingecko_cache = {}
COINGECKO_CACHE_TTL = 3600  # 1 hour
# CoinGecko platforms(contract-by-chain) cache: asset -> {ts, coin_id, platforms}
coingecko_platforms_cache = {}
COINGECKO_PLATFORMS_TTL = 6 * 3600  # 6 hours
# CoinGecko contract->coin_id cache: "contract:{platform}:{addr}" -> {ts, coin_id}
coingecko_contract_cache = {}
# Кэш статуса депозитов/выводов по монете (asset -> {ts, data})
asset_status_cache = {}
dex_quote_cache = {}
interchain_scan_cache = OrderedDict()
# Server-side TTLs. Values are defaults — runtime code should pull overrides
# from Config (e.g. bot_instance.config.get("dex_quote_ttl_sec")) where the
# UI team needs to tweak them without editing Python.
#
# ASSET_STATUS_TTL is intentionally short: deposit/withdraw flags flip during
# exchange maintenance (Gate.io/MEXC toggle several times per day), so we
# cannot afford to hand out 20-minute-old "open" flags when the wallet is
# already closed. The prefetch loop repopulates this cache every ~15s per
# batch, so 10 min is a comfortable ceiling. Contract addresses and CoinGecko
# platform mappings live in *separate* caches with longer TTLs.
#
# INTERCHAIN_SCAN_TTL lowered to 60s so DEX/bridge prices stay fresher.
# DEX_QUOTE_TTL lowered to 30s — DEX pools can re-price in ~1 block.
ASSET_STATUS_TTL = 600  # 10 минут (deposit/withdraw flags)
DEX_QUOTE_TTL = 30
INTERCHAIN_SCAN_TTL = 60
ASSET_STATUS_PREFETCH_BATCH = 5
ASSET_STATUS_PREFETCH_PAUSE_SEC = 2.0
ASSET_STATUS_PREFETCH_INTERVAL_SEC = 15.0
asset_status_prefetch_thread = None
EXCHANGE_CONTRACT_WARMUP_BATCH = 2
EXCHANGE_CONTRACT_WARMUP_PAUSE_SEC = 3.0
EXCHANGE_CONTRACT_WARMUP_INTERVAL_SEC = 20.0
EXCHANGE_CONTRACT_CACHE_MAX_AGE_SEC = 7 * 24 * 3600
exchange_contract_warmup_thread = None
kraken_kyber_index_thread = None
kraken_kyber_index_lock = threading.Lock()
kraken_kyber_scan_cache_live = {"payload": None, "ts": 0}
kraken_kyber_scan_thread = None

from utils.api_manager import api_manager
app.api_manager = api_manager

@app.route('/')
def index():
    """Main page of the web interface."""
    vite_index = os.path.join(VITE_DIST_DIR, 'index.html')
    if os.path.exists(vite_index):
        return send_from_directory(VITE_DIST_DIR, 'index.html')
    # Frontend build missing: keep the backend running, but make the issue obvious.
    return (
        "<!doctype html><html><head><meta charset='utf-8'/>"
        "<title>Frontend Not Built</title></head><body>"
        "<h2>Frontend build not found</h2>"
        "<p>Run: <code>cd web_interface/frontend && npm install && npm run build</code></p>"
        "</body></html>",
        503,
        {"Content-Type": "text/html; charset=utf-8"},
    )

@app.route('/assets/<path:filename>')
def vite_assets(filename):
    """Serve Vite-built assets if present."""
    if os.path.isdir(VITE_ASSETS_DIR):
        return send_from_directory(VITE_ASSETS_DIR, filename)
    abort(404)

@app.route('/api/data')
def get_data():
    """API for getting current data."""
    global current_data, bot_instance
    
    return jsonify(current_data)

def _normalize_exchange_info_row_prefetch(row: Dict[str, Any], asset: str) -> Dict[str, Any]:
    def _to_bool(v):
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            if v == 1:
                return True
            if v == 0:
                return False
        s = str(v).strip().lower()
        if s in ('1', 'true', 'yes', 'y', 'on', 'open', 'enabled'):
            return True
        if s in ('0', 'false', 'no', 'n', 'off', 'closed', 'disabled'):
            return False
        return None
    return {
        'exchange': row.get('exchange'),
        'asset': row.get('asset') or asset,
        'chain': row.get('chain') or '-',
        'deposit_enabled': _to_bool(row.get('deposit_enabled')),
        'withdraw_enabled': _to_bool(row.get('withdraw_enabled')),
        'withdraw_fee': row.get('withdraw_fee'),
        'min_withdraw': row.get('min_withdraw'),
        'contract_address': row.get('contract') or row.get('contract_address') or row.get('tokenAddress'),
    }

async def _prefetch_asset_status_async(asset: str, enabled_exchanges: List[Any]) -> List[Dict[str, Any]]:
    try:
        from utils.exchange_info import exchange_info_fetcher
        info = await exchange_info_fetcher.get_all_exchange_info(asset)
        rows = info.get('exchanges', []) if isinstance(info, dict) else []
    except Exception as e:
        logging.debug(f"asset_status prefetch failed for {asset}: {e}")
        rows = []
    data = []
    for row in rows:
        if isinstance(row, dict):
            data.append(_normalize_exchange_info_row_prefetch(row, asset))
    fetched = {str(d.get('exchange', '')).lower() for d in data if isinstance(d, dict)}
    # Если нет ни одного известного статуса, не трогаем кэш
    has_known = any(
        isinstance(d, dict) and (
            d.get('deposit_enabled') is not None or d.get('withdraw_enabled') is not None
        ) for d in data
    )
    for ex in enabled_exchanges:
        try:
            name = ex.name
        except Exception:
            continue
        if name.lower() not in fetched:
            data.append({
                'exchange': name,
                'asset': asset,
                'chain': '-',
                'deposit_enabled': None,
                'withdraw_enabled': None,
                'withdraw_fee': None,
                'min_withdraw': None,
                'contract_address': None
            })
    if not has_known:
        return []
    return data

def _asset_status_prefetch_loop(bot):
    queue = deque()
    queued = set()
    while is_running:
        try:
            if not bot or not getattr(bot, 'loop', None):
                time.sleep(2)
                continue
            # Собираем активы из текущих возможностей
            assets = set()
            try:
                ops = bot.cached_opportunities or []
                for opp in ops:
                    sym = str(opp.get('symbol') or '')
                    asset = extract_base_asset(sym, assume_pair=True) if sym else ''
                    if asset:
                        assets.add(asset)
            except Exception:
                assets = set()
            if not assets:
                time.sleep(2)
                continue
            # Обновляем очередь только для устаревших/отсутствующих
            now = time.time()
            for asset in assets:
                cached = asset_status_cache.get(asset)
                if cached and (now - cached.get('ts', 0) < ASSET_STATUS_TTL):
                    continue
                if asset not in queued:
                    queue.append(asset)
                    queued.add(asset)
            # Обрабатываем батчами
            if not queue:
                time.sleep(ASSET_STATUS_PREFETCH_INTERVAL_SEC)
                continue
            # Список бирж для плейсхолдеров
            try:
                enabled_exchanges = bot.calc.get_enabled_exchanges() if bot.calc else getattr(bot, 'exchanges', [])
            except Exception:
                enabled_exchanges = []
            batch = min(ASSET_STATUS_PREFETCH_BATCH, len(queue))
            for _ in range(batch):
                asset = queue.popleft()
                queued.discard(asset)
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        _prefetch_asset_status_async(asset, enabled_exchanges),
                        bot.loop
                    )
                    data = fut.result(timeout=10)
                    if data:
                        asset_status_cache[asset] = {'ts': time.time(), 'data': data}
                except Exception:
                    continue
                time.sleep(ASSET_STATUS_PREFETCH_PAUSE_SEC)
        except Exception:
            time.sleep(2)


def _collect_exchange_contract_warmup_assets(bot) -> List[str]:
    assets: List[str] = []
    seen = set()
    opportunity_assets = set()

    def add_asset(asset: str) -> None:
        asset_u = str(asset or '').strip().upper()
        if not asset_u or asset_u in seen:
            return
        seen.add(asset_u)
        assets.append(asset_u)

    try:
        for opp in getattr(bot, 'cached_opportunities', []) or []:
            asset = extract_base_asset(opp.get('symbol'), assume_pair=True)
            asset_u = str(asset or '').strip().upper()
            if asset_u:
                opportunity_assets.add(asset_u)
            add_asset(asset)
    except Exception:
        pass

    try:
        calc_obj = getattr(bot, 'calc', None)
        exchange_objs = calc_obj.get_enabled_exchanges() if calc_obj and hasattr(calc_obj, 'get_enabled_exchanges') else []
    except Exception:
        exchange_objs = []

    for exchange in exchange_objs or []:
        try:
            pair_list = sorted(list(getattr(exchange, 'available_pairs', set()) or []))
        except Exception:
            pair_list = []
        for symbol in pair_list:
            base, _quote = split_pair_symbol(symbol)
            if base:
                add_asset(base)

    try:
        calc_obj = getattr(bot, 'calc', None)
        common_pairs = sorted(list(getattr(calc_obj, 'common_pairs', set()) or [])) if calc_obj else []
    except Exception:
        common_pairs = []
    for symbol in common_pairs:
        base, _quote = split_pair_symbol(symbol)
        if base:
            add_asset(base)

    priority_assets = {str(item).strip().upper() for item in CONTRACT_FIRST_PRIORITY_ASSETS}
    stable_assets = {str(item).strip().upper() for item in STABLE_ASSETS}
    assets.sort(
        key=lambda asset: (
            3 if asset in stable_assets else 0,
            2 if asset in priority_assets else 0,
            1 if asset in opportunity_assets else 0,
            -len(asset),
            asset,
        ),
        reverse=True,
    )
    return assets


def _exchange_contract_warmup_loop(bot):
    queue = deque()
    queued = set()
    while is_running:
        try:
            if not bot:
                time.sleep(2)
                continue
            try:
                stale_after = int(bot.config.get('exchange_metadata_cache_ttl_sec', EXCHANGE_CONTRACT_CACHE_MAX_AGE_SEC) or EXCHANGE_CONTRACT_CACHE_MAX_AGE_SEC)
            except Exception:
                stale_after = EXCHANGE_CONTRACT_CACHE_MAX_AGE_SEC

            assets = _collect_exchange_contract_warmup_assets(bot)
            if not assets:
                time.sleep(5)
                continue

            for asset in assets:
                if exchange_info_fetcher.is_asset_fresh_in_cache(asset, stale_after):
                    continue
                if asset not in queued:
                    queue.append(asset)
                    queued.add(asset)

            if not queue:
                time.sleep(EXCHANGE_CONTRACT_WARMUP_INTERVAL_SEC)
                continue

            batch_assets: List[str] = []
            batch = min(EXCHANGE_CONTRACT_WARMUP_BATCH, len(queue))
            for _ in range(batch):
                asset = queue.popleft()
                queued.discard(asset)
                batch_assets.append(asset)

            try:
                stats = asyncio.run(
                    exchange_info_fetcher.warm_assets(
                        batch_assets,
                        max_age_sec=stale_after,
                        pause_sec=EXCHANGE_CONTRACT_WARMUP_PAUSE_SEC,
                        force_refresh=False,
                        limit=len(batch_assets),
                    )
                )
                logging.info(
                    "exchange contract warmup: processed=%s network=%s cache=%s errors=%s remaining_queue=%s",
                    stats.get('assets_processed'),
                    stats.get('network_fetches'),
                    stats.get('cache_hits'),
                    stats.get('errors'),
                    len(queue),
                )
            except Exception as exc:
                logging.debug("exchange contract warmup batch failed: %s", exc)

            time.sleep(EXCHANGE_CONTRACT_WARMUP_INTERVAL_SEC)
        except Exception:
            time.sleep(2)

@app.route('/api/status_test')
def get_status_test():
    return jsonify({"marker": "LATEST_VERSION_V2", "status": "ok"})

@app.route('/api/status')
def get_status():
    """API for getting bot status."""
    global bot_instance
    if bot_instance:
        try:
            calc_obj = getattr(bot_instance, 'calc', None)
            enabled_exchanges = []
            if calc_obj:
                if hasattr(calc_obj, 'get_enabled_exchanges'):
                    enabled_exchanges = [ex.name for ex in calc_obj.get_enabled_exchanges()]
                elif hasattr(calc_obj, 'exchanges'):
                    enabled_exchanges = [ex.name for ex in calc_obj.exchanges if getattr(ex, 'enabled', False)]
            
            # Если в calc нет бирж, пробуем взять их напрямую у бота
            if not enabled_exchanges:
                bot_exchanges = getattr(bot_instance, 'exchanges', None)
                if bot_exchanges:
                    enabled_exchanges = [ex.name for ex in bot_exchanges if getattr(ex, 'enabled', False)]

            last_ts = 0.0
            try:
                last_ts = float(getattr(bot_instance, 'last_update_time', 0.0) or 0.0)
            except Exception:
                last_ts = 0.0
            age_sec = int(max(0.0, time.time() - last_ts)) if last_ts > 0 else None
            try:
                stale_after = float(bot_instance.config.get("monitor_interval", 60) or 60) * 2.0
            except Exception:
                stale_after = 120.0

            ws_health = _get_ws_health()
            ws_enabled = _ws_ui_enabled()
            ws_port_running = _ws_port_open() if ws_enabled else False
            return jsonify({
                "running": bot_instance.running,
                "status": bot_instance.status_var.get() if hasattr(bot_instance, 'status_var') else "Running",
                "common_pairs": len(calc_obj.common_pairs) if calc_obj and hasattr(calc_obj, 'common_pairs') else 0,
                "enabled_exchanges": enabled_exchanges,
                "ws_enabled": ws_enabled,
                "ws_running": bool((ws_health and ws_health.get("running")) or ws_port_running),
                "ws_health_stale": bool(ws_port_running and not ws_health),
                "ws_quotes": ws_health.get("ws_quotes") if ws_health else 0,
                "ws_quotes_by_exchange": ws_health.get("ws_quotes_by_exchange") if ws_health else {},
                "last_update": datetime.fromtimestamp(last_ts).strftime('%H:%M:%S') if last_ts > 0 else "",
                "last_update_ts": last_ts if last_ts > 0 else None,
                "last_update_age_sec": age_sec,
                "last_update_stale": bool(age_sec is not None and age_sec >= stale_after),
                "total_opportunities": len(bot_instance.cached_opportunities) if hasattr(bot_instance, 'cached_opportunities') else 0
            })
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logging.error(f"DEBUG_MARKER_V2: CRITICAL ERROR in get_status: {e}\n{tb}")
            return jsonify({"error": f"v2_error_{str(e)}", "traceback": tb})
    # Fallback: return current_data snapshot instead of error so UI doesn't break
    return jsonify({
        "running": False,
        "status": current_data.get("status", "Stopped"),
        "common_pairs": current_data.get("common_pairs", 0),
        "enabled_exchanges": current_data.get("enabled_exchanges", []),
        "last_update": current_data.get("last_update", ""),
        "last_update_ts": None,
        "last_update_age_sec": None,
        "last_update_stale": True,
        "total_opportunities": len(current_data.get("opportunities", []))
    })

@app.route('/api/dashboard')
def get_dashboard():
    """API for получения данных for дашборда."""
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "error": "Бот not инициализирован"})
    
    try:
        # Получаем статистику по спредам
        opportunities = bot_instance.cached_opportunities if hasattr(bot_instance, 'cached_opportunities') else []
        
        # Статистика по спредам
        spread_stats = {
            "high": len([o for o in opportunities if o["spread"] >= 5.0]),
            "medium": len([o for o in opportunities if 1.0 <= o["spread"] < 5.0]),
            "low": len([o for o in opportunities if o["spread"] < 1.0]),
            "total": len(opportunities)
        }
        
        # Статистика по биржам
        exchange_stats = {}
        for opp in opportunities:
            buy_ex = opp["buy_exchange"]
            sell_ex = opp["sell_exchange"]
            
            if buy_ex not in exchange_stats:
                exchange_stats[buy_ex] = {"buy": 0, "sell": 0}
            if sell_ex not in exchange_stats:
                exchange_stats[sell_ex] = {"buy": 0, "sell": 0}
            
            exchange_stats[buy_ex]["buy"] += 1
            exchange_stats[sell_ex]["sell"] += 1
        
        # Статистика по символам (топ-5)
        symbol_stats = {}
        for opp in opportunities:
            symbol = opp["symbol"]
            if symbol not in symbol_stats:
                symbol_stats[symbol] = 0
            symbol_stats[symbol] += 1
        
        top_symbols = sorted(symbol_stats.items(), key=lambda x: x[1], reverse=True)[:5]
        
        # Статус бирж
        exchange_status = []
        calc_obj = getattr(bot_instance, 'calc', None)
        exchanges_to_check = []
        if calc_obj and hasattr(calc_obj, 'exchanges') and calc_obj.exchanges:
            exchanges_to_check = calc_obj.exchanges
        else:
            exchanges_to_check = getattr(bot_instance, 'exchanges', []) or []

        for ex in exchanges_to_check:
            exchange_status.append({
                "name": getattr(ex, 'name', 'Unknown'),
                "enabled": getattr(ex, 'enabled', False),
                "error_count": getattr(ex, 'error_count', 0)
            })
        
        return jsonify({
            "success": True,
            "data": {
                "spread_stats": spread_stats,
                "exchange_stats": exchange_stats,
                "top_symbols": top_symbols,
                "exchange_status": exchange_status,
                "running": bot_instance.running,
                "status": bot_instance.status_var.get(),
                "common_pairs": bot_instance.shared_data.get_common_pairs() if hasattr(bot_instance, 'shared_data') and bot_instance.shared_data else (len(bot_instance.calc.common_pairs) if hasattr(bot_instance.calc, 'common_pairs') else 0),
                "last_update": datetime.fromtimestamp(bot_instance.last_update_time).strftime('%H:%M:%S') if hasattr(bot_instance, 'last_update_time') and bot_instance.last_update_time else ""
            }
        })
    except Exception as e:
        logging.error(f"Error getting data for dashboard: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/start', methods=['POST'])
def start_bot():
    """API for запуска бота."""
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "error": "Бот не инициализирован"})
    try:
        started = bool(bot_instance.start_monitoring())
        if started:
            return jsonify({"success": True, "message": "Бот запущен"})
        return jsonify({"success": False, "error": "Бот уже запущен или ещё не завершил прошлый старт"})
    except Exception as e:
        logging.error(f"Ошибка при запуске бота через API: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/stop', methods=['POST'])
def stop_bot():
    """API for остановки бота."""
    global bot_instance
    if bot_instance and bot_instance.running:
        try:
            bot_instance.stop_monitoring()
            return jsonify({"success": True, "message": "Бот остановлен"})
        except Exception as e:
            logging.error(f"Ошибка при остановке бота через API: {e}\n{traceback.format_exc()}")
            return jsonify({"success": False, "error": str(e)})
    return jsonify({"success": False, "error": "Бот уже остановлен or not инициализирован"})

@app.route('/api/update_pairs', methods=['POST'])
def update_pairs():
    """API for обновления пар."""
    global bot_instance
    if bot_instance and bot_instance.running:
        try:
            # Запускаем in отдельном потоке, чтобы not блокировать ответ API
            threading.Thread(target=bot_instance.update_pairs_manual, daemon=True).start()
            return jsonify({"success": True, "message": "Обновление пар запущено"})
        except Exception as e:
            logging.error(f"Ошибка при обновлении пар через API: {e}\n{traceback.format_exc()}")
            return jsonify({"success": False, "error": str(e)})
    return jsonify({"success": False, "error": "Бот not запущен or not инициализирован"})

@app.route('/api/settings', methods=['GET', 'POST'])
def settings():
    """API for получения and изменения настроек."""
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "error": "Бот not инициализирован"})
    
    if request.method == 'GET':
        # Получаем текущие настройки
        try:
            # Список доступных бирж
            calc_obj = getattr(bot_instance, 'calc', None)
            exchanges_to_list = []
            if calc_obj and hasattr(calc_obj, 'exchanges') and calc_obj.exchanges:
                exchanges_to_list = calc_obj.exchanges
            else:
                exchanges_to_list = getattr(bot_instance, 'exchanges', []) or []
                
            available_exchanges = []
            seen_exchange_names = set()
            for ex in exchanges_to_list:
                name = str(getattr(ex, 'name', '') or '').strip()
                if not name or name.lower() == 'dummy':
                    continue
                lowered = name.lower()
                if lowered in seen_exchange_names:
                    continue
                seen_exchange_names.add(lowered)
                available_exchanges.append(name)
            
            # Получаем список включенных бирж or все биржи, if список пуст
            enabled_exchanges = bot_instance.config.get("enabled_exchanges", [])
            if not enabled_exchanges:
                enabled_exchanges = available_exchanges
            else:
                allowed = {name.lower() for name in available_exchanges}
                enabled_exchanges = [name for name in enabled_exchanges if str(name).strip().lower() in allowed]
            
            effective_scan_interval = bot_instance.config.get(
                "monitor_interval",
                bot_instance.config.get("ui_polling_interval_sec", 7),
            )

            return jsonify({
                "min_spread": bot_instance.config.get("min_spread", 0.5),
                "max_spread": bot_instance.config.get("max_spread", 50.0),
                "enabled_exchanges": enabled_exchanges,
                "available_exchanges": available_exchanges,
                "coingecko_api_key": bot_instance.config.get("coingecko_api_key", ""),
                # Core bot flags
                "use_orderbooks": bot_instance.config.get("use_orderbooks", False),
                "orderbooks_refine_top_symbols": bot_instance.config.get("orderbooks_refine_top_symbols", 5),
                "orderbooks_per_exchange_timeout_sec": bot_instance.config.get("orderbooks_per_exchange_timeout_sec", 8.0),
                "tickers_per_exchange_timeout_sec": bot_instance.config.get("tickers_per_exchange_timeout_sec", 12.0),
                "stale_rank_penalty_enabled": bot_instance.config.get("stale_rank_penalty_enabled", False),
                "stale_rank_penalty_grace_sec": bot_instance.config.get("stale_rank_penalty_grace_sec", 10.0),
                "stale_rank_penalty_per_min_pct": bot_instance.config.get("stale_rank_penalty_per_min_pct", 0.2),
                "stale_rank_hide_after_sec": bot_instance.config.get("stale_rank_hide_after_sec", 0.0),
                # UI flags for columns
                "ui_show_momentum_1m": bot_instance.config.get("ui_show_momentum_1m", True),
                "ui_show_momentum_15m": bot_instance.config.get("ui_show_momentum_15m", True),
                "ui_show_heat": bot_instance.config.get("ui_show_heat", True),
                "ui_show_dispersion": bot_instance.config.get("ui_show_dispersion", True),
                "ui_show_cg_vol24": bot_instance.config.get("ui_show_cg_vol24", True),
                "ui_show_cg_mcap": bot_instance.config.get("ui_show_cg_mcap", True),
                "ui_group_by_liquidity": bot_instance.config.get("ui_group_by_liquidity", False),
                "ui_group_by_symbol": bot_instance.config.get("ui_group_by_symbol", False),
                "ui_show_direction": bot_instance.config.get("ui_show_direction", False),
                "ui_arb_filter_transfer": bot_instance.config.get("ui_arb_filter_transfer", False),
                "ui_arb_filter_transfer_strict_unknown": bot_instance.config.get("ui_arb_filter_transfer_strict_unknown", False),
                "ui_arb_filter_liquidity": bot_instance.config.get("ui_arb_filter_liquidity", False),
                "ui_popover_min_profit_pct": bot_instance.config.get("ui_popover_min_profit_pct", 0.0),
                "arb_min_notional_usd": bot_instance.config.get("arb_min_notional_usd", 300.0),
                "ui_arb_top_liquidity_n": bot_instance.config.get("ui_arb_top_liquidity_n", 0),
                "kraken_kyber_enabled": bot_instance.config.get("kraken_kyber_enabled", True),
                "kraken_kyber_min_spread": bot_instance.config.get("kraken_kyber_min_spread", 0.5),
                "kraken_kyber_notional_usd": bot_instance.config.get("kraken_kyber_notional_usd", 250.0),
                # This field is shown in UI as the main refresh interval.
                # Keep it aligned with real scanner interval.
                "ui_polling_interval_sec": effective_scan_interval,
                "ui_use_separate_polling": bot_instance.config.get("ui_use_separate_polling", False),
                "ui_polling_interval_status_sec": bot_instance.config.get("ui_polling_interval_status_sec", 7),
                "ui_polling_interval_opportunities_sec": bot_instance.config.get("ui_polling_interval_opportunities_sec", 7),
                "direction": bot_instance.config.get("direction", {
                    "dir_up_thresh": 0.25,
                    "dir_down_thresh": 0.25,
                    "dir_up_strong": 0.25,
                    "use_direction_in_label": True,
                    "weights": {"m1": 0.5, "m5": 0.7, "spike": 0.5, "slope": 0.3},
                    "strength_thresholds": [0.25, 0.5, 0.75],
                    "colors": ["secondary", "warning", "success"],
                    "conf_min_to_show": 0.4,
                    "conf_boosts": {"vs1": 0.15, "vs5": 0.15, "heat": 0.1},
                    "conf_for_label": 0.6,
                    "high_conf_threshold": 0.8,
                    "high_conf_weight_boost": 1.3
                }),
            })
        except Exception as e:
            logging.error(f"Ошибка при получении настроек: {e}\n{traceback.format_exc()}")
            return jsonify({"success": False, "error": str(e)})
    else:
        # Изменяем настройки
        try:
            data = request.get_json(silent=True, force=False) or {}
            # Валидируем min/max спред в связке, чтобы не сохранять неконсистентные значения.
            try:
                current_min = float(bot_instance.config.get("min_spread", 0.5))
            except Exception:
                current_min = 0.5
            try:
                current_max = float(bot_instance.config.get("max_spread", 50.0))
            except Exception:
                current_max = 50.0

            new_min = current_min
            new_max = current_max
            try:
                if "min_spread" in data:
                    new_min = float(data.get("min_spread"))
                if "max_spread" in data:
                    new_max = float(data.get("max_spread"))
            except Exception:
                return jsonify({"success": False, "error": "Некорректное значение min/max спреда"})

            if new_min < 0:
                return jsonify({"success": False, "error": "min_spread не может быть отрицательным"})
            if new_max <= new_min:
                return jsonify({"success": False, "error": "max_spread должен быть больше min_spread"})
            if "min_spread" in data:
                try:
                    val = float(data.get("min_spread"))
                    bot_instance.min_spread_var.set(str(val))
                    bot_instance.config.set("min_spread", val)
                    logging.info(f"Updated min_spread to {val}")
                except Exception: pass
            if "max_spread" in data:
                try:
                    val = float(data.get("max_spread"))
                    bot_instance.max_spread_var.set(str(val))
                    bot_instance.config.set("max_spread", val)
                except Exception: pass
            if "enabled_exchanges" in data and isinstance(data.get("enabled_exchanges"), list):
                enabled = [str(x) for x in data.get("enabled_exchanges", []) if x]
                for exch_name, var in bot_instance.exchange_vars.items():
                    var.set(str(exch_name in enabled))
                # Сохраняем и применяем сразу (не полагаемся на GUI save_settings).
                bot_instance.config.set("enabled_exchanges", enabled)
                try:
                    for ex in getattr(bot_instance, 'calc', None).exchanges:
                        ex.enabled = ex.name in enabled
                except Exception:
                    logging.exception("Не удалось применить enabled_exchanges к калькулятору")
            if "coingecko_api_key" in data:
                bot_instance.config.set("coingecko_api_key", data.get("coingecko_api_key", "")) 

            # Core bot flags
            if "use_orderbooks" in data:
                bot_instance.config.set("use_orderbooks", bool(data.get("use_orderbooks")))
            if "orderbooks_refine_top_symbols" in data:
                try:
                    v = int(float(data.get("orderbooks_refine_top_symbols")))
                    v = max(0, min(400, v))
                    bot_instance.config.set("orderbooks_refine_top_symbols", v)
                except Exception:
                    logging.exception("Invalid orderbooks_refine_top_symbols")
            if "orderbooks_per_exchange_timeout_sec" in data:
                try:
                    v = float(data.get("orderbooks_per_exchange_timeout_sec"))
                    # Reasonable bounds to prevent runaway waits
                    if v < 1:
                        v = 1.0
                    if v > 30:
                        v = 30.0
                    bot_instance.config.set("orderbooks_per_exchange_timeout_sec", v)
                except Exception:
                    logging.exception("Invalid orderbooks_per_exchange_timeout_sec")

            if "tickers_per_exchange_timeout_sec" in data:
                try:
                    v = float(data.get("tickers_per_exchange_timeout_sec"))
                    if v < 2:
                        v = 2.0
                    if v > 60:
                        v = 60.0
                    bot_instance.config.set("tickers_per_exchange_timeout_sec", v)
                except Exception:
                    logging.exception("Invalid tickers_per_exchange_timeout_sec")

            if "stale_rank_penalty_enabled" in data:
                bot_instance.config.set("stale_rank_penalty_enabled", bool(data.get("stale_rank_penalty_enabled")))
            if "stale_rank_penalty_grace_sec" in data:
                try:
                    v = float(data.get("stale_rank_penalty_grace_sec"))
                    if v < 0:
                        v = 0.0
                    if v > 600:
                        v = 600.0
                    bot_instance.config.set("stale_rank_penalty_grace_sec", v)
                except Exception:
                    logging.exception("Invalid stale_rank_penalty_grace_sec")
            if "stale_rank_penalty_per_min_pct" in data:
                try:
                    v = float(data.get("stale_rank_penalty_per_min_pct"))
                    if v < 0:
                        v = 0.0
                    if v > 50:
                        v = 50.0
                    bot_instance.config.set("stale_rank_penalty_per_min_pct", v)
                except Exception:
                    logging.exception("Invalid stale_rank_penalty_per_min_pct")
            if "stale_rank_hide_after_sec" in data:
                try:
                    v = float(data.get("stale_rank_hide_after_sec"))
                    if v < 0:
                        v = 0.0
                    if v > 3600:
                        v = 3600.0
                    bot_instance.config.set("stale_rank_hide_after_sec", v)
                except Exception:
                    logging.exception("Invalid stale_rank_hide_after_sec")

            # UI flags for columns
            for key in [
                "ui_show_momentum_1m",
                "ui_show_momentum_15m",
                "ui_show_heat",
                "ui_show_dispersion",
                "ui_show_cg_vol24",
                "ui_show_cg_mcap",
                "ui_group_by_liquidity",
                "ui_group_by_symbol",
                "ui_show_direction",
                "ui_arb_filter_transfer",
                "ui_arb_filter_transfer_strict_unknown",
                "ui_arb_filter_liquidity",
                "ws_use_for_ui",
                "ws_require_top_liquidity",
                "ws_ui_auto_start",
                "ui_popover_min_profit_pct",
            ]:
                if key in data:
                    if key == "ui_popover_min_profit_pct":
                        try:
                            v = float(data.get(key))
                            if v >= 0:
                                bot_instance.config.set(key, v)
                        except Exception:
                            logging.exception("Неверное значение ui_popover_min_profit_pct")
                    else:
                        bot_instance.config.set(key, bool(data.get(key)))

            if bool(bot_instance.config.get("ui_arb_filter_liquidity", False)):
                # Liquidity is only known after bid/ask orderbook refinement.
                # Without this, the UI filter has no source data and can hide everything.
                bot_instance.config.set("use_orderbooks", True)
                try:
                    current_top = int(float(bot_instance.config.get("orderbooks_refine_top_symbols", 5) or 5))
                except Exception:
                    current_top = 5
                if current_top <= 0:
                    bot_instance.config.set("orderbooks_refine_top_symbols", 5)

            # Интервал обновления UI (секунды)
            # Also synchronize backend scanner cadence with this value so UI and core stay aligned.
            if "ui_polling_interval_sec" in data:
                try:
                    val = float(data.get("ui_polling_interval_sec"))
                    if val >= 1:
                        bot_instance.config.set("ui_polling_interval_sec", val)
                        bot_instance.config.set("monitor_interval", val)
                        bot_instance.config.set("update_interval", val)
                except Exception:
                    logging.exception("Неверное значение ui_polling_interval_sec")

            if "arb_min_notional_usd" in data:
                try:
                    val = float(data.get("arb_min_notional_usd"))
                    if val >= 0:
                        bot_instance.config.set("arb_min_notional_usd", val)
                except Exception:
                    logging.exception("Неверное значение arb_min_notional_usd")

            if "kraken_kyber_enabled" in data:
                bot_instance.config.set("kraken_kyber_enabled", bool(data.get("kraken_kyber_enabled")))
            if "kraken_kyber_min_spread" in data:
                try:
                    val = float(data.get("kraken_kyber_min_spread"))
                    if val >= 0:
                        bot_instance.config.set("kraken_kyber_min_spread", val)
                except Exception:
                    logging.exception("Invalid kraken_kyber_min_spread")
            if "kraken_kyber_notional_usd" in data:
                try:
                    val = float(data.get("kraken_kyber_notional_usd"))
                    if val > 0:
                        bot_instance.config.set("kraken_kyber_notional_usd", val)
                except Exception:
                    logging.exception("Invalid kraken_kyber_notional_usd")

            if "ui_arb_top_liquidity_n" in data:
                try:
                    val = int(float(data.get("ui_arb_top_liquidity_n")))
                    if val >= 0:
                        bot_instance.config.set("ui_arb_top_liquidity_n", val)
                except Exception:
                    logging.exception("Неверное значение ui_arb_top_liquidity_n")

            # Раздельные интервалы обновления UI
            try:
                if "ui_use_separate_polling" in data:
                    bot_instance.config.set("ui_use_separate_polling", bool(data.get("ui_use_separate_polling")))
                if "ui_polling_interval_status_sec" in data:
                    v = float(data.get("ui_polling_interval_status_sec"))
                    if v >= 1:
                        bot_instance.config.set("ui_polling_interval_status_sec", v)
                if "ui_polling_interval_opportunities_sec" in data:
                    v = float(data.get("ui_polling_interval_opportunities_sec"))
                    if v >= 1:
                        bot_instance.config.set("ui_polling_interval_opportunities_sec", v)
                        if bool(data.get("ui_use_separate_polling", bot_instance.config.get("ui_use_separate_polling", False))):
                            # In separate mode, opportunities interval is the closest match to scanner cadence.
                            bot_instance.config.set("monitor_interval", v)
                            bot_instance.config.set("update_interval", v)
            except Exception:
                logging.exception("Неверные значения раздельных интервалов UI")
            # Секция direction — допускаем приход целиком
            if isinstance(data.get("direction"), dict):
                bot_instance.config.set("direction", data.get("direction"))

            return jsonify({"success": True, "message": "Настройки сохранены"})
        except Exception as e:
            logging.error(f"Ошибка при изменении настроек через API: {e}\n{traceback.format_exc()}")
            return jsonify({"success": False, "error": str(e)})

@app.route('/api/opportunities')
def get_opportunities():
    """API for getting current arbitrage opportunities with sorting and filtering."""
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "error": "Bot not initialized"})
    
    try:
        # Получаем параметры запроса
        min_spread = float(request.args.get('min_spread', 0))
        symbol_filter = request.args.get('symbol', '').upper()
        sort_by = request.args.get('sort', 'spread')  # По умолчанию сортируем по спреду
        sort_order = request.args.get('order', 'desc')  # По умолчанию по убыванию

        # Фильтруем данные
        filtered_data = []
        ws_health = None
        source_mode = "rest_fallback"
        try:
            ws_rows, ws_health = _get_ws_opportunities(min_spread=min_spread, limit=300)
            legacy_rows = []
            if hasattr(bot_instance, 'shared_data') and bot_instance.shared_data:
                opportunities_data = bot_instance.shared_data.get_opportunities()
                logging.debug("Retrieved %s opportunities from shared_data", len(opportunities_data) if opportunities_data else 0)
                legacy_rows = opportunities_data.copy() if opportunities_data else []
            elif hasattr(bot_instance, 'cached_opportunities'):
                logging.debug("shared_data not available, trying cached_opportunities")
                if bot_instance.cached_opportunities:
                    logging.debug("bot_instance.cached_opportunities length: %s", len(bot_instance.cached_opportunities))
                    legacy_rows = bot_instance.cached_opportunities.copy()
                else:
                    logging.warning("bot_instance.cached_opportunities is empty or None")
            else:
                logging.error("Neither shared_data nor cached_opportunities available")

            if ws_health is not None:
                filtered_data = self_or_rows_merge(ws_rows, legacy_rows)
                source_mode = "hybrid" if legacy_rows else "websocket"
                logging.debug(
                    "Retrieved %s opportunities for display (%s WS + %s legacy)",
                    len(filtered_data),
                    len(ws_rows),
                    len(legacy_rows),
                )
            else:
                filtered_data = legacy_rows
                logging.debug("Retrieved %s legacy opportunities for display", len(filtered_data))
        except Exception as e:
            logging.error(f"Error getting opportunities: {e}\n{traceback.format_exc()}")
            filtered_data = []

        # Фильтр по спреду
        filtered_data = [opp for opp in filtered_data if opp["spread"] >= min_spread]

        # Фильтр по символу
        if symbol_filter:
            filtered_data = [opp for opp in filtered_data if symbol_filter in opp["symbol"]]

        # Фильтр по черному списку
        if hasattr(bot_instance, 'blacklist_manager'):
            filtered_data = [opp for opp in filtered_data if not bot_instance.blacklist_manager.is_blacklisted(opp["symbol"])]

        filter_transfer = bot_instance.config.get("ui_arb_filter_transfer", False)
        strict_unknown = bot_instance.config.get("ui_arb_filter_transfer_strict_unknown", False)
        if filter_transfer:
            now_ts = time.time()
            status_cache = {}

            def _ex_key(name: str) -> str:
                return "".join(ch for ch in str(name or "").lower() if ch.isalnum())

            def _summarize_bool(rows: List[dict], field: str) -> Optional[bool]:
                if not rows:
                    return None
                values = [r.get(field) for r in rows if isinstance(r, dict)]
                if any(v is True for v in values):
                    return True
                has_null = any(v is None for v in values)
                has_false = any(v is False for v in values)
                if has_false and not has_null:
                    return False
                return None

            def _status_map_for_asset(asset: str) -> Optional[Dict[str, dict]]:
                if asset in status_cache:
                    return status_cache[asset]
                cached = asset_status_cache.get(asset)
                if not cached or (now_ts - cached.get('ts', 0) >= ASSET_STATUS_TTL):
                    status_cache[asset] = None
                    return None
                rows = cached.get('data') or []
                by_key: Dict[str, List[dict]] = {}
                for row in rows:
                    k = _ex_key(row.get('exchange'))
                    if not k:
                        continue
                    by_key.setdefault(k, []).append(row)
                summary = {}
                for k, items in by_key.items():
                    summary[k] = {
                        "deposit": _summarize_bool(items, "deposit_enabled"),
                        "withdraw": _summarize_bool(items, "withdraw_enabled"),
                    }
                status_cache[asset] = summary
                return summary

            filtered = []
            for opp in filtered_data:
                sym = opp.get("symbol")
                if not sym:
                    filtered.append(opp)
                    continue
                asset = extract_base_asset(sym, assume_pair=True)
                status_map = _status_map_for_asset(asset)
                if not status_map:
                    if strict_unknown:
                        continue
                    filtered.append(opp)
                    continue
                buy_key = _ex_key(opp.get("buy_exchange"))
                sell_key = _ex_key(opp.get("sell_exchange"))
                buy_st = status_map.get(buy_key, {})
                sell_st = status_map.get(sell_key, {})
                buy_w = buy_st.get("withdraw")
                sell_d = sell_st.get("deposit")
                if strict_unknown:
                    if buy_w is not True or sell_d is not True:
                        continue
                else:
                    if buy_w is False or sell_d is False:
                        continue
                filtered.append(opp)
            filtered_data = filtered

        filter_liquidity = bot_instance.config.get("ui_arb_filter_liquidity", False)
        min_notional_usd = bot_instance.config.get("arb_min_notional_usd", 300.0)
        if filter_liquidity:
            try:
                min_req = float(min_notional_usd)
            except Exception:
                min_req = 300.0
            try:
                min_spread_req = float(bot_instance.config.get("min_spread", 0.0) or 0.0)
            except Exception:
                min_spread_req = 0.0
            kept = []
            for opp in filtered_data:
                depth_spread = opp.get("depth_gross_spread_pct")
                if opp.get("depth_executable") is True and isinstance(depth_spread, (int, float)):
                    if depth_spread >= min_spread_req:
                        kept.append(opp)
                    continue
                liq = opp.get("min_liquidity_usd")
                if not isinstance(liq, (int, float)):
                    # With the liquidity filter enabled, unknown orderbook liquidity is not proof.
                    # Hide ticker-only rows instead of showing phantom spreads as executable.
                    continue
                if liq >= min_req:
                    kept.append(opp)
            filtered_data = kept

        top_liquidity_n = bot_instance.config.get("ui_arb_top_liquidity_n", 0)
        try:
            top_n = int(float(top_liquidity_n))
        except Exception:
            top_n = 0
        if top_n and top_n > 0:
            best_by_symbol = {}
            for opp in filtered_data:
                sym = opp.get("symbol")
                if not sym:
                    continue
                liq = opp.get("min_liquidity_usd")
                if not isinstance(liq, (int, float)) or liq <= 0:
                    continue
                best_by_symbol[sym] = max(liq, best_by_symbol.get(sym, 0))
            top_symbols = {
                sym for sym, _ in sorted(best_by_symbol.items(), key=lambda x: x[1], reverse=True)[:top_n]
            }
            if top_symbols:
                filtered_data = [opp for opp in filtered_data if opp.get("symbol") in top_symbols]

        # Сортировка
        if sort_by == 'symbol':
            filtered_data.sort(key=lambda x: x["symbol"], reverse=(sort_order == 'desc'))
        elif sort_by == 'buy_price':
            filtered_data.sort(key=lambda x: x["buy_price"], reverse=(sort_order == 'desc'))
        elif sort_by == 'sell_price':
            filtered_data.sort(key=lambda x: x["sell_price"], reverse=(sort_order == 'desc'))
        elif sort_by == 'momentum':
            # Сортировка по импульсу, если поле присутствует
            def momentum_key(x):
                m = x.get('momentum_pct')
                return (m if isinstance(m, (int, float)) else -1e9)
            filtered_data.sort(key=momentum_key, reverse=(sort_order == 'desc'))
        elif sort_by == 'momentum5':
            # Сортировка по 5-минутному импульсу; по модулю, чтобы наверх попадали и сильные падения
            def momentum5_key(x):
                m = x.get('momentum_5m_pct')
                if isinstance(m, (int, float)):
                    return abs(m)
                return -1e9
            filtered_data.sort(key=momentum5_key, reverse=(sort_order == 'desc'))
        elif sort_by == 'momentum1':
            # Сортировка по 1-минутному импульсу (если доступен); по модулю
            def momentum1_key(x):
                m = x.get('momentum_1m_pct')
                if isinstance(m, (int, float)):
                    return abs(m)
                return -1e9
            filtered_data.sort(key=momentum1_key, reverse=(sort_order == 'desc'))
        elif sort_by == 'momentum15':
            # Сортировка по 15-минутному импульсу (если доступен); по модулю
            def momentum15_key(x):
                m = x.get('momentum_15m_pct')
                if isinstance(m, (int, float)):
                    return abs(m)
                return -1e9
            filtered_data.sort(key=momentum15_key, reverse=(sort_order == 'desc'))
        elif sort_by == 'heat':
            # Сортировка по интегральному скору тепла/интересности
            def heat_key(x):
                h = x.get('heat_score')
                if isinstance(h, (int, float)):
                    return h
                return -1e9
            filtered_data.sort(key=heat_key, reverse=(sort_order == 'desc'))
        elif sort_by == 'dispersion':
            # Сортировка по текущей кросс-биржевой дисперсии
            def disp_key(x):
                d = x.get('dispersion_pct')
                if isinstance(d, (int, float)):
                    return d
                return -1e9
            filtered_data.sort(key=disp_key, reverse=(sort_order == 'desc'))
        elif sort_by == 'cgvol24':
            # Сортировка по суточному объему CoinGecko
            def cgvol_key(x):
                v = x.get('cg_volume_24h_usd')
                if isinstance(v, (int, float)):
                    return v
                return -1e18
            filtered_data.sort(key=cgvol_key, reverse=(sort_order == 'desc'))
        elif sort_by == 'cgmcap':
            # Сортировка по рыночной капитализации CoinGecko
            def cgmcap_key(x):
                v = x.get('cg_market_cap_usd')
                if isinstance(v, (int, float)):
                    return v
                return -1e18
            filtered_data.sort(key=cgmcap_key, reverse=(sort_order == 'desc'))
        elif sort_by == 'direction':
            # Сортировка по силе короткого направления (по модулю score)
            def dir_key(x):
                s = x.get('direction_score')
                if isinstance(s, (int, float)):
                    return abs(s)
                return -1e9
            filtered_data.sort(key=dir_key, reverse=(sort_order == 'desc'))
        elif sort_by == 'liquidity':
            # Сортировка по тьеру ликвидности (высокая -> средняя -> низкая -> нет данных)
            def tier_val(o):
                try:
                    vol = o.get('cg_volume_24h_usd')
                    mcap = o.get('cg_market_cap_usd')
                    vol = float(vol) if isinstance(vol, (int, float)) else None
                    mcap = float(mcap) if isinstance(mcap, (int, float)) else None
                    high = (vol is not None and vol >= 5_000_000) or (mcap is not None and mcap >= 300_000_000)
                    mid = (vol is not None and vol >= 500_000) or (mcap is not None and mcap >= 50_000_000)
                    if high:
                        return 0
                    if mid:
                        return 1
                    if vol is None and mcap is None:
                        return 3
                    return 2
                except Exception:
                    return 3
            # Внутри одного тьера — сортируем по Vol24, затем по MCAP, затем по спреду
            def liq_key(x):
                t = tier_val(x)
                vol = x.get('cg_volume_24h_usd') or -1
                mcap = x.get('cg_market_cap_usd') or -1
                spread = x.get('spread') or -1
                # Для обратной сортировки по vol/mcap/spread — используем отрицательные значения
                return (t, -float(vol) if isinstance(vol, (int, float)) else 0.0,
                        -float(mcap) if isinstance(mcap, (int, float)) else 0.0,
                        -float(spread) if isinstance(spread, (int, float)) else 0.0)
            filtered_data.sort(key=liq_key)
        else:  # По умолчанию сортируем по спреду
            filtered_data.sort(key=lambda x: x["spread"], reverse=(sort_order == 'desc'))

        combined = filtered_data

        # ВАЖНО: UI `/api/opportunities` вызывается часто (polling), поэтому здесь нельзя
        # дергать CoinGecko в реальном времени — это приводит к 429 и подвисаниям.
        # Берём vol24/mcap только из локального кэша бота (обновляется в фоне батчами).
        try:
            feat_cache = getattr(bot_instance, "_cg_feat_cache", None)
            if isinstance(feat_cache, dict) and combined:
                now_ts = time.time()
                try:
                    ttl = int(bot_instance.config.get("cg_feature_ttl_sec", 600) or 600)
                except Exception:
                    ttl = 600

                for o in combined:
                    try:
                        if o.get("cg_volume_24h_usd") is not None and o.get("cg_market_cap_usd") is not None:
                            continue
                        sym = o.get("symbol")
                        base = extract_base_asset(sym, assume_pair=True)
                        if not base:
                            continue
                        cached = feat_cache.get(str(base).upper()) or feat_cache.get(str(sym).upper())
                        if not cached:
                            continue
                        ts = cached.get("ts") or 0
                        if ttl > 0 and (now_ts - float(ts or 0)) > ttl:
                            continue
                        if o.get("cg_volume_24h_usd") is None:
                            o["cg_volume_24h_usd"] = cached.get("vol24")
                        if o.get("cg_market_cap_usd") is None:
                            o["cg_market_cap_usd"] = cached.get("mcap")
                    except Exception:
                        continue
        except Exception:
            logging.debug("CG cache enrichment skipped", exc_info=True)

        last_ts = 0.0
        if source_mode == "websocket" and combined:
            try:
                last_ts = max(float(o.get("last_update_ts") or o.get("timestamp") or 0.0) for o in combined)
            except Exception:
                last_ts = 0.0
        if last_ts <= 0:
            try:
                last_ts = float(getattr(bot_instance, 'last_update_time', 0.0) or 0.0)
            except Exception:
                last_ts = 0.0
        age_sec = int(max(0.0, time.time() - last_ts)) if last_ts > 0 else None
        try:
            stale_after = float(bot_instance.config.get("monitor_interval", 60) or 60) * 2.0
        except Exception:
            stale_after = 120.0

        return jsonify({
            "success": True,
            "data": combined,
            "source": source_mode,
            "ws_health": ws_health,
            "last_update": datetime.fromtimestamp(last_ts).strftime('%H:%M:%S') if last_ts > 0 else "",
            "last_update_ts": last_ts if last_ts > 0 else None,
            "last_update_age_sec": age_sec,
            "last_update_stale": bool(age_sec is not None and age_sec >= stale_after),
        })
    except Exception as e:
        logging.error(f"Error getting arbitrage opportunities via API: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)})



def _sync_permanent_blacklist_to_calculator() -> None:
    global bot_instance
    try:
        manager = getattr(bot_instance, "blacklist_manager", None)
        calc = getattr(bot_instance, "calc", None)
        if manager is not None and calc is not None and hasattr(calc, "set_permanent_blacklist"):
            calc.set_permanent_blacklist(getattr(manager, "permanent_blacklist", set()))
    except Exception as e:
        try:
            logging.warning("Failed to sync permanent blacklist to calculator: %s", e)
        except Exception:
            pass

def _purge_symbol_from_runtime_cache(symbol: str) -> int:
    """Remove a newly blacklisted symbol from live UI caches immediately."""
    global bot_instance
    target = str(symbol or "").strip().upper()
    if not target or not bot_instance:
        return 0
    removed = 0

    def keep(row):
        nonlocal removed
        try:
            if str(row.get("symbol", "")).strip().upper() == target:
                removed += 1
                return False
        except Exception:
            pass
        return True

    for attr in ("cached_opportunities", "opportunities"):
        try:
            rows = getattr(bot_instance, attr, None)
            if isinstance(rows, list):
                setattr(bot_instance, attr, [row for row in rows if keep(row)])
        except Exception:
            pass

    try:
        sd = getattr(bot_instance, "shared_data", None)
        if isinstance(sd, dict):
            for key in ("opportunities", "cached_opportunities"):
                rows = sd.get(key)
                if isinstance(rows, list):
                    sd[key] = [row for row in rows if keep(row)]
    except Exception:
        pass

    return removed

@app.route('/api/blacklist', methods=['GET', 'POST', 'DELETE'])
def manage_blacklist():
    """API for управления черным списком."""
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "error": "Бот not инициализирован"})
    
    if request.method == 'GET':
        # Получаем информацию о черном списке
        try:
            if hasattr(bot_instance.blacklist_manager, 'get_blacklist_info'):
                blacklist_info = bot_instance.blacklist_manager.get_blacklist_info()
            else:
                # Альтернативная реализация, if метод not существует
                blacklist_info = {
                    "permanent_list": bot_instance.blacklist_manager.permanent_blacklist,
                    "temporary_list": {k: v.strftime('%Y-%m-%d %H:%M:%S') 
                                      for k, v in bot_instance.blacklist_manager.temporary_blacklist.items()}
                }
            return jsonify({"success": True, "data": blacklist_info})
        except Exception as e:
            logging.error(f"Ошибка при получении информации о черном списке: {e}\n{traceback.format_exc()}")
            return jsonify({"success": False, "error": str(e)})
    
    elif request.method == 'POST':
        # Добавляем монету in черный список
        try:
            data = request.get_json(silent=True, force=False) or {}
            symbol = (data.get("symbol") or "").strip().upper()
            # Поддерживаем оба формата: duration ("1h"|"24h"|"permanent") и hours/permanent
            duration = data.get("duration")
            permanent_flag = data.get("permanent")
            hours_val = data.get("hours")

            if not symbol:
                return jsonify({"success": False, "error": "Символ не указан"})

            message = None
            # 1) Если пришла duration-строка — используем её
            if isinstance(duration, str):
                dur = duration.lower().strip()
                if dur == "1h":
                    bot_instance.blacklist_manager.add_temporary(symbol, duration_minutes=60)
                    message = f"Символ {symbol} добавлен в черный список на 1 час"
                elif dur == "24h":
                    bot_instance.blacklist_manager.add_temporary(symbol, duration_minutes=1440)
                    message = f"Символ {symbol} добавлен в черный список на 24 часа"
                elif dur == "permanent":
                    bot_instance.blacklist_manager.add_permanent(symbol)
                    message = f"Символ {symbol} добавлен в постоянный черный список"
                else:
                    return jsonify({"success": False, "error": f"Недопустимый срок блокировки: {duration}"})
            else:
                # 2) Гибкий формат: permanent/hours
                if isinstance(permanent_flag, bool) and permanent_flag:
                    bot_instance.blacklist_manager.add_permanent(symbol)
                    message = f"Символ {symbol} добавлен в постоянный черный список"
                else:
                    # hours может быть строкой/числом
                    try:
                        hours_int = int(hours_val) if hours_val is not None else 24
                    except Exception:
                        hours_int = 24
                    # Минимум 1 час
                    if hours_int <= 0:
                        hours_int = 1
                    bot_instance.blacklist_manager.add_temporary(symbol, duration_minutes=hours_int * 60)
                    # Сообщение с корректным склонением часов не критично — оставим простую форму
                    message = f"Символ {symbol} добавлен в черный список на {hours_int} часов"

            _sync_permanent_blacklist_to_calculator()
            purged = _purge_symbol_from_runtime_cache(symbol)
            try:
                logging.info("Blacklist runtime purge for %s removed %s cached rows", symbol, purged)
            except Exception:
                pass
            return jsonify({"success": True, "message": message, "purged_cached_rows": purged})
        except Exception as e:
            logging.error(f"Ошибка при добавлении монеты в черный список: {e}\n{traceback.format_exc()}")
            return jsonify({"success": False, "error": str(e)})
    
    elif request.method == 'DELETE':
        # Удаляем монету из черного списка
        try:
            symbol = request.args.get("symbol")
            if not symbol:
                return jsonify({"success": False, "error": "Символ not указан"})
            
            if bot_instance.blacklist_manager.remove(symbol):
                return jsonify({"success": True, "message": f"Символ {symbol} удален из черного списка"})
            else:
                return jsonify({"success": False, "error": f"Символ {symbol} not найден in черном списке"})
        except Exception as e:
            logging.error(f"Ошибка при удалении монеты из черного списка: {e}\n{traceback.format_exc()}")
            return jsonify({"success": False, "error": str(e)})

# Обработчик for ошибок 404 and 500
@app.errorhandler(404)
def not_found(error):
    return render_template('500.html', description=str(error)), 404

@app.errorhandler(500)
def server_error(error):
    return render_template('500.html', description=str(error)), 500


# ==================== API для управления API ключами бирж ====================

@app.route('/api/exchange_api_keys', methods=['GET', 'POST'])
def manage_exchange_api_keys():
    """
    API для управления API ключами бирж.
    
    GET: Возвращает список настроенных бирж (без раскрытия ключей)
    POST: Устанавливает API ключи для биржи
    """
    global bot_instance
    if not bot_instance:
        return jsonify({"error": "Бот не инициализирован"}), 503
    
    if request.method == 'GET':
        try:
            config_keys = bot_instance.config.get("exchange_api_keys", {})
            configured = {}
            for exchange, keys in (config_keys or {}).items():
                if isinstance(keys, dict) and keys.get("api_key"):
                    configured[exchange] = {
                        "configured": True,
                        "api_key_preview": keys.get("api_key", "")[:8] + "..." if keys.get("api_key") else None
                    }
                else:
                    configured[exchange] = {"configured": False}
            
            return jsonify({
                "success": True,
                "exchanges": configured,
                "supported": ["MEXC", "Bybit", "CoinEx"]
            })
        except Exception as e:
            logging.error(f"Ошибка при получении API ключей: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
    
    else:  # POST
        try:
            data = request.get_json(silent=True) or {}
            exchange = data.get("exchange", "").strip()
            api_key = data.get("api_key", "").strip()
            secret = data.get("secret", "").strip()
            
            if not exchange or not api_key or not secret:
                return jsonify({
                    "success": False,
                    "error": "Требуется указать exchange, api_key и secret"
                }), 400
            
            # Получаем текущие ключи
            config_keys = bot_instance.config.get("exchange_api_keys", {}) or {}
            config_keys[exchange] = {
                "api_key": api_key,
                "secret": secret
            }
            
            # Сохраняем
            bot_instance.config.set("exchange_api_keys", config_keys)
            bot_instance.config.save()
            
            return jsonify({
                "success": True,
                "message": f"API ключи для {exchange} сохранены"
            })
        except Exception as e:
            logging.error(f"Ошибка при сохранении API ключей: {e}\n{traceback.format_exc()}")
            return jsonify({"success": False, "error": str(e)}), 500


# Новый API эндпоинт for получения информации о монете с последовательным использованием API:
@app.route('/api/coingecko_info/<path:symbol>')
def get_coingecko_info(symbol):
    """
    Получает детальную информацию о монете с последовательным использованием API:
    1. CoinGecko Public API
    2. CoinGecko Pro API (если указан ключ)
    3. CoinMarketCap API (только если явно включён в конфиге)
    """
    if not bot_instance:
        logging.error("[CG_INFO] Бот не инициализирован")
        return jsonify({"error": "Сервер не готов", "message": "Бот не инициализирован"}), 503
        
    if not hasattr(bot_instance, 'coingecko'):
        logging.error("[CG_INFO] CoinGecko клиент не инициализирован")
        return jsonify({"error": "Сервер не готов", "message": "CoinGecko клиент не настроен"}), 503
    
    # Очистка символа до базового ассета (BTCUSDT/BTC-USDT/BTC_USDT -> BTC)
    original_symbol = symbol
    clean_symbol = extract_base_asset(symbol, assume_pair=True)
    if not clean_symbol:
        clean_symbol = str(symbol).upper()
    
    logging.info(f"[CG_INFO] Запрос информации: оригинал='{original_symbol}', очищенный='{clean_symbol}', Args={dict(request.args)}")
    
    # Получаем данные о биржах and объемах из запроса
    buy_exchange = request.args.get('buy_ex', '')
    sell_exchange = request.args.get('sell_ex', '')
    buy_p = request.args.get('buy_p')
    sell_p = request.args.get('sell_p')
    exchanges_param = request.args.get('exchanges', '')
    
    # Формируем словарь с объемами торгов
    exchange_volumes = {}
    try:
        # Проверяем объемы in запросе
        buy_volume_str = request.args.get('buy_volume')
        sell_volume_str = request.args.get('sell_volume')
        
        if buy_volume_str and buy_exchange:
            try:
                buy_volume = float(buy_volume_str)
                if buy_volume > 0:
                    exchange_volumes[buy_exchange.lower()] = buy_volume
            except ValueError:
                logging.warning(f"[CG_INFO] Некорректный объем покупки: {buy_volume_str}")
                
        if sell_volume_str and sell_exchange:
            try:
                sell_volume = float(sell_volume_str)
                if sell_volume > 0:
                    exchange_volumes[sell_exchange.lower()] = sell_volume
            except ValueError:
                logging.warning(f"[CG_INFO] Некорректный объем продажи: {sell_volume_str}")
                
    except Exception as e:
        logging.error(f"[CG_INFO] Ошибка при обработке объемов торгов: {e}")
    
    error_messages = []
    
    try:
        # Контекст для более точного выбора coin_id при неоднозначном символе
        ex_names: List[str] = []
        try:
            if exchanges_param:
                ex_names = [x.strip() for x in str(exchanges_param).split(",") if x.strip()]
        except Exception:
            ex_names = []
        if not ex_names:
            ex_names = [x for x in [buy_exchange, sell_exchange] if x]

        ref_price_usd = None
        try:
            _, quote = split_pair_symbol(original_symbol)
            stable_quotes = {"USDT", "USDC", "USD", "BUSD", "TUSD", "FDUSD", "DAI"}
            if quote and quote.upper() in stable_quotes:
                bp = float(buy_p) if buy_p is not None else None
                sp = float(sell_p) if sell_p is not None else None
                if bp and sp and bp > 0 and sp > 0:
                    ref_price_usd = (bp + sp) / 2.0
                elif bp and bp > 0:
                    ref_price_usd = bp
                elif sp and sp > 0:
                    ref_price_usd = sp
        except Exception:
            ref_price_usd = None

        # 1. Пробуем получить данные через CoinGecko (автоматически попробует Pro and Public API)
        try:
            coin_data = bot_instance.coingecko.get_coingecko_data_for_symbol_sync(
                clean_symbol,
                exchange_volumes,
                include_tickers=False,
                exchange_names=ex_names or None,
                reference_price_usd=ref_price_usd,
                price_tolerance=0.25,
            )
            if coin_data:
                logging.info(f"[CG_INFO] Успешно получены данные для '{clean_symbol}' (coin_id={coin_data.get('id', 'unknown')})")
                if 'description' in coin_data:
                    del coin_data['description']
                return jsonify(coin_data)
            else:
                error_messages.append(f"CoinGecko API не нашёл монету '{clean_symbol}'")
                logging.warning(f"[CG_INFO] Монета не найдена: '{clean_symbol}' (оригинал: '{original_symbol}')")
        except Exception as e:
            error_msg = f"Ошибка при получении данных CoinGecko: {str(e)}"
            logging.error(f"[CG_INFO] {error_msg} для {clean_symbol}", exc_info=True)
            error_messages.append(error_msg)
        
        # Ранее здесь выполнялся фоллбэк к CoinMarketCap, но адаптер удалён.
        error_details = " | ".join(error_messages)
        logging.warning(f"[CG_INFO] Не удалось получить данные для {clean_symbol}. Причины: {error_details}")
        return jsonify({
            "error": "Не удалось получить данные",
            "symbol": original_symbol,
            "clean_symbol": clean_symbol,
            "details": error_details,
            "message": f"Информация о монете '{clean_symbol}' недоступна в CoinGecko"
        }), 200  # Возвращаем 200 вместо 404, чтобы клиент мог обработать ошибку
    except Exception as fatal_e:
        logging.critical(f"[CG_INFO] Критическая ошибка эндпоинта: {fatal_e}", exc_info=True)
        return jsonify({
            "error": "Критическая ошибка сервера",
            "message": "Ошибка при обработке запроса информации о монете",
            "details": str(fatal_e)
        }), 200

@app.route('/api/coingecko_chart/<path:symbol>')
def get_coingecko_chart(symbol):
    """Возвращает компактную историю цен CoinGecko для символа + синтетическую лестницу уровней.

    Query params:
      days: int|str — период (по умолчанию 1)
      interval: minutely|hourly|daily (опционально)
      baseline: float — базовая цена для расчёта уровней
      levels: int — желаемое число уровней в лестнице (3..5)
      min_pct: float — нижний порог процента (например, 3)
      max_pct: float — верхний порог процента (например, 50)
    """
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "error": "Бот не инициализирован"}), 200
    try:
        days = request.args.get('days', '1')
        interval = request.args.get('interval') or None
        baseline = request.args.get('baseline')
        levels = int(request.args.get('levels', '5') or 5)
        min_pct = float(request.args.get('min_pct', '3') or 3.0)
        max_pct = float(request.args.get('max_pct', '50') or 50.0)
        asset_symbol = extract_base_asset(symbol, assume_pair=True) or str(symbol).upper()
        # Санитизация
        if levels < 3:
            levels = 3
        if levels > 5:
            levels = 5
        if min_pct < 0:
            min_pct = 0.0
        if max_pct < min_pct:
            max_pct = min_pct

        # Подсказки по объёмам из последней возможности для символа (если есть)
        exchange_volumes = {}
        try:
            # Попробуем найти недавнюю возможность для этого символа и вытащить buy/sell volume
            recent_opps = []
            if hasattr(bot_instance, 'shared_data') and bot_instance.shared_data:
                recent_opps = bot_instance.shared_data.get_opportunities() or []
            elif hasattr(bot_instance, 'cached_opportunities') and bot_instance.cached_opportunities:
                recent_opps = bot_instance.cached_opportunities or []
            for o in recent_opps:
                try:
                    if extract_base_asset(o.get('symbol'), assume_pair=True).upper() != str(asset_symbol).upper():
                        continue

                    be = o.get('buy_exchange'); se = o.get('sell_exchange')
                    bv = float(o.get('buy_volume')) if o.get('buy_volume') is not None else None
                    sv = float(o.get('sell_volume')) if o.get('sell_volume') is not None else None
                    if be and isinstance(bv, float) and bv > 0:
                        exchange_volumes[be.lower()] = bv
                    if se and isinstance(sv, float) and sv > 0:
                        exchange_volumes[se.lower()] = sv
                    break
                except Exception:
                    continue
        except Exception:
            exchange_volumes = {}

        # Достаём CG историю
        if not hasattr(bot_instance, 'coingecko'):
            from utils.coingecko import CoinGecko
            bot_instance.coingecko = CoinGecko(bot_instance.config)
        chart = bot_instance.coingecko.get_market_chart_for_symbol_sync(
            asset_symbol,
            vs_currency='usd',
            days=days,
            interval=interval,
            exchange_volumes=exchange_volumes or None,
        )
        prices = []
        if isinstance(chart, dict) and isinstance(chart.get('prices'), list):
            # Формат CG: [[ts_ms, price], ...]
            prices = chart['prices']

        # Синтетическая лестница уровней на основе baseline
        ladder = []
        try:
            base = float(baseline) if baseline is not None else None
        except Exception:
            base = None
        if base and base > 0:
            # Распределим уровни равномерно от min_pct до max_pct
            if levels == 1:
                pct_list = [min_pct]
            else:
                step = (max_pct - min_pct) / (levels - 1) if levels > 1 else 0
                pct_list = [min_pct + i * step for i in range(levels)]
            for pct in pct_list:
                up_price = base * (1.0 + pct / 100.0)
                down_price = base * (1.0 - pct / 100.0)
                ladder.append({
                    "pct": pct,
                    "up": up_price,
                    "down": down_price,
                })

        return jsonify({
            "success": True,
            "symbol": symbol,
            "asset": asset_symbol,
            "prices": prices,  # [[ts_ms, price], ...]
            "ladder": ladder,  # [{pct, up, down}, ...]
        })
    except Exception as e:
        logging.error(f"Ошибка /api/coingecko_chart для {symbol}: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 200

@app.route('/api/update_coingecko_list', methods=['POST'])
def update_coingecko_list():
    """Обновляет список монет CoinGecko."""
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "error": "Бот not инициализирован"})
    
    try:
        if not hasattr(bot_instance, 'coingecko'):
            # Если объекта нет, создаем его
            from utils.coingecko import CoinGecko
            bot_instance.coingecko = CoinGecko(bot_instance.config)
            logging.info("CoinGecko клиент создан")
          # Запускаем асинхронное обновление списка in отдельном потоке
        def update_list_thread():
            if not hasattr(bot_instance, 'loop') or not bot_instance.loop:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            else:
                loop = bot_instance.loop
                
            try:
                # Обновляем список монет
                logging.info("Запуск обновления списка монет CoinGecko...")
                updater = getattr(bot_instance.coingecko, 'update_coins_list', None) or getattr(bot_instance.coingecko, '_update_coins_list', None)
                if updater is None:
                    raise AttributeError('CoinGecko updater is not available')
                future = asyncio.run_coroutine_threadsafe(updater(force=True), loop)
                success = future.result(timeout=30)  # Ждем результат с таймаутом
                
                if success:
                    logging.info(f"Список монет CoinGecko обновлен успешно ({len(bot_instance.coingecko.coins_list)} монет)")
                else:
                    logging.error("Не удалось обновить список монет CoinGecko")
                    
                # Для надежности else раз загружаем из кэша
                bot_instance.coingecko._load_list_from_cache()
                logging.info(f"Статус после обновления: {len(bot_instance.coingecko.coins_list)} монет, "
                             f"{len(bot_instance.coingecko.coins_list_by_symbol)} символов, "
                             f"{len(bot_instance.coingecko.coins_list_by_id)} ID")
            except Exception as e:
                logging.error(f"Ошибка при обновлении списка монет CoinGecko: {e}\n{traceback.format_exc()}")
                
        # Запускаем поток обновления
        threading.Thread(target=update_list_thread, daemon=True).start()
        
        return jsonify({"success": True, "message": "Запущено обновление списка монет CoinGecko"})
    except Exception as e:
        logging.error(f"Ошибка при запуске обновления списка монет CoinGecko: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/refresh_coingecko', methods=['POST'])
def refresh_coingecko_list():
    """API for обновления списка монет CoinGecko."""
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "error": "Бот not инициализирован"})
    
    try:
        # Импортируем модуль for обновления списка монет
        from web_interface.update_coingecko import update_coingecko_list_async
        
        # Запускаем обновление
        result = update_coingecko_list_async(bot_instance)
        if result:
            return jsonify({"success": True, "message": "Запущено обновление списка монет CoinGecko"})
        else:
            return jsonify({"success": False, "error": "Ошибка при запуске обновления списка монет"})
    except Exception as e:
        logging.error(f"Ошибка при обновлении списка монет CoinGecko: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/orderbooks/<path:symbol>')
def get_orderbooks(symbol):
    """Возвращает топ стаканов (best bid/ask и объёмы) по всем включённым биржам для указанного символа."""
    global bot_instance
    try:
        if not bot_instance:
            return jsonify({"success": False, "error": "Бот не инициализирован"}), 400
        if not getattr(bot_instance, 'running', False) or not getattr(bot_instance, 'loop', None):
            return jsonify({"success": False, "error": "Бот не запущен"}), 400
        if not getattr(bot_instance, 'session', None):
            return jsonify({"success": False, "error": "HTTP-сессия недоступна"}), 400
        if not hasattr(bot_instance, 'calc'):
            return jsonify({"success": False, "error": "Калькулятор не инициализирован"}), 400
        
        # Запрашиваем книги ордеров для конкретного символа через существующий event loop бота
        async def fetch():
            try:
                calc_obj = getattr(bot_instance, 'calc', None)
                if calc_obj and hasattr(calc_obj, 'fetch_order_books_for'):
                    ex_raw = request.args.get("exchanges", "") or ""
                    wanted = [x.strip() for x in ex_raw.split(",") if x and x.strip()]
                    return await calc_obj.fetch_order_books_for(
                        bot_instance.session,
                        [symbol],
                        exchange_names=wanted or None,
                        per_exchange_timeout_sec=6,
                    )
                return []
            except Exception as e:
                logging.error(f"Ошибка при получении стаканов для {symbol}: {e}\n{traceback.format_exc()}")
                return {}
        
        fut = asyncio.run_coroutine_threadsafe(fetch(), bot_instance.loop)
        try:
            result = fut.result(timeout=10)
        except Exception as te:
            logging.error(f"/api/orderbooks timeout for {symbol}: {te}")
            # Возвращаем дружелюбный ответ вместо 500, чтобы панель info не падала
            return jsonify({
                "success": False,
                "error": "timeout",
                "data": []
            })
        
        # Приводим к плоскому списку для удобства фронтенда
        flat = []
        for ex_name, sym_map in (result or {}).items():
            if isinstance(sym_map, dict) and symbol in sym_map and isinstance(sym_map[symbol], dict):
                ob = sym_map[symbol]
                bid = ob.get('bid'); ask = ob.get('ask')
                bid_v = ob.get('bid_volume'); ask_v = ob.get('ask_volume')
                spread_pct = None
                try:
                    if bid and ask and bid > 0 and ask > 0:
                        spread_pct = ((ask - bid) / ask) * 100.0
                except Exception:
                    spread_pct = None
                flat.append({
                    'exchange': ex_name,
                    'symbol': symbol,
                    'bid': bid,
                    'ask': ask,
                    'bid_volume': bid_v,
                    'ask_volume': ask_v,
                    'spread_percent': spread_pct
                })
        
        # Сортируем по наименьшему спреду (внутренний рынок более «узкий»)
        flat.sort(key=lambda x: (999 if x.get('spread_percent') is None else x['spread_percent']))
        return jsonify({"success": True, "data": flat})
    except Exception as e:
        logging.error(f"Критическая ошибка /api/orderbooks/{symbol}: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e), "data": []})

@app.route('/api/coin_full_info/<path:symbol>')
def get_coin_full_info(symbol):
    """Возвращает полную информацию о монете со всех бирж:
    - Статус депозитов/выводов по каждой бирже и сети
    - Контракты на каждой сети
    - Ссылки на DEX (GeckoTerminal, DexScreener)
    
    Использует аутентифицированные API для надежности.
    """
    global bot_instance
    try:
        asset = extract_base_asset(symbol, assume_pair=True) if symbol else ''
        if not asset:
            return jsonify({"success": False, "error": "Некорректный символ"}), 400
        
        # Import our new module
        from utils.exchange_info import get_coin_info
        
        # Get event loop from bot or create new one
        loop = None
        if bot_instance and hasattr(bot_instance, 'loop') and bot_instance.loop:
            loop = bot_instance.loop
        
        async def fetch():
            # Если это контракт, сначала пытаемся найти ассет через CG
            target_asset = asset
            if asset.startswith('0x') or len(asset) > 30:
                if not hasattr(bot_instance, 'coingecko'):
                    from utils.coingecko import CoinGecko
                    bot_instance.coingecko = CoinGecko(bot_instance.config)
                
                cg_data = bot_instance.coingecko.get_coingecko_data_for_symbol_sync(asset, include_tickers=False)
                if cg_data and isinstance(cg_data, dict) and (cg_data.get('symbol') or cg_data.get('ticker')):
                    target_asset = (cg_data.get('symbol') or cg_data.get('ticker')).upper()
            
            return await get_coin_info(target_asset)
        
        if loop and loop.is_running():
            # Run in bot's event loop
            future = asyncio.run_coroutine_threadsafe(fetch(), loop)
            try:
                result = future.result(timeout=15)
            except Exception as e:
                logging.error(f"Timeout getting coin info for {asset}: {e}")
                result = {"ticker": asset, "exchanges": [], "dex_links": [], "contracts": {}}
        else:
            # Run in new event loop
            try:
                result = asyncio.run(fetch())
            except Exception as e:
                logging.error(f"Error getting coin info for {asset}: {e}")
                result = {"ticker": asset, "exchanges": [], "dex_links": [], "contracts": {}}
        
        return jsonify({
            "success": True,
            "asset": asset,
            "exchanges": result.get("exchanges", []),
            "dex_links": result.get("dex_links", []),
            "contracts": result.get("contracts", {}),
        })
    except Exception as e:
        logging.error(f"Критическая ошибка /api/coin_full_info/{symbol}: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/asset_status_batch')
def get_asset_status_batch():
    """Батч-эндпоинт: возвращает asset_status для нескольких монет одним запросом.

    Параметры:
      ?assets=BTC,ETH,SPK      — до 20 символов через запятую
      &include_contracts=1     — прогреть CoinGecko кеш для контрактов
      &overall_timeout=5       — общий таймаут на одну монету (по умолчанию 5с)

    Ответ:
      {success: true, data: {BTC: [...], ETH: [...], SPK: [...]}, cache_hits: N, cache_misses: M}

    Используется UI для предзагрузки топ-N строк таблицы (см. prefetchTopAssets в app.js).
    Кладётся в тот же `asset_status_cache` что и обычный /api/asset_status — поэтому
    последующий одиночный запрос той же монеты ответит мгновенно из кэша.
    """
    global bot_instance, asset_status_cache
    try:
        if not bot_instance or not getattr(bot_instance, 'loop', None):
            return jsonify({"success": False, "error": "Бот не инициализирован"}), 400

        raw_assets = request.args.get('assets', '') or ''
        # Разделители: запятая/пробел/точка-с-запятой.
        parts = [p.strip() for p in re.split(r'[,\s;]+', raw_assets) if p and p.strip()]
        # extract_base_asset: USDT-пары → базовый тикер.
        normalized: List[str] = []
        seen = set()
        for p in parts:
            base = extract_base_asset(p, assume_pair=True) if p else ''
            if base and base.upper() not in seen:
                normalized.append(base.upper())
                seen.add(base.upper())
            if len(normalized) >= 20:
                break

        if not normalized:
            return jsonify({"success": False, "error": "Параметр 'assets' пуст", "data": {}}), 400

        now = time.time()
        include_contracts = str(request.args.get('include_contracts', '0')).lower() in ('1', 'true', 'yes')
        force_refresh = str(request.args.get('force', '0')).lower() in ('1', 'true', 'yes')
        try:
            overall_timeout = float(request.args.get('overall_timeout', '5') or 5.0)
        except Exception:
            overall_timeout = 5.0
        overall_timeout = max(2.0, min(overall_timeout, 15.0))

        enabled_objs = []
        try:
            if hasattr(bot_instance, 'calc') and bot_instance.calc:
                enabled_objs = bot_instance.calc.get_enabled_exchanges()
            else:
                enabled_objs = list(getattr(bot_instance, 'exchanges', []) or [])
        except Exception:
            enabled_objs = list(getattr(bot_instance, 'exchanges', []) or [])

        result: Dict[str, List[Dict[str, Any]]] = {}
        cache_hits = 0
        cache_misses = 0
        to_fetch: List[str] = []

        for asset in normalized:
            cached = asset_status_cache.get(asset)
            if (not force_refresh) and cached and (now - cached.get('ts', 0) < ASSET_STATUS_TTL):
                result[asset] = list(cached.get('data') or [])
                cache_hits += 1
            else:
                to_fetch.append(asset)
                cache_misses += 1

        if to_fetch:
            logging.info(f"[BATCH] prefetch {len(to_fetch)} assets ({to_fetch}), timeout={overall_timeout}s each")

            async def _batch_fetch():
                tasks = {
                    asset: asyncio.ensure_future(
                        asyncio.wait_for(
                            _prefetch_asset_status_async(asset, enabled_objs),
                            timeout=overall_timeout,
                        )
                    )
                    for asset in to_fetch
                }
                out: Dict[str, List[Dict[str, Any]]] = {}
                for asset, task in tasks.items():
                    try:
                        data = await task
                        out[asset] = list(data or [])
                    except asyncio.TimeoutError:
                        logging.warning(f"[BATCH] timeout for {asset}")
                        out[asset] = []
                    except Exception as exc:
                        logging.warning(f"[BATCH] fetch failed for {asset}: {exc}")
                        out[asset] = []
                return out

            try:
                future = asyncio.run_coroutine_threadsafe(_batch_fetch(), bot_instance.loop)
                fetched = future.result(timeout=overall_timeout * len(to_fetch) + 3)
            except Exception as exc:
                logging.error(f"[BATCH] global failure: {exc}")
                fetched = {asset: [] for asset in to_fetch}

            for asset, rows in fetched.items():
                result[asset] = rows
                if rows:
                    # Кладём в тот же кэш что и /api/asset_status — одиночный запрос ответит моментально.
                    asset_status_cache[asset] = {"ts": time.time(), "data": rows}

        # По желанию догреем CoinGecko платформы в фоне — не блокируем ответ.
        if include_contracts:
            try:
                async def _bg_prefetch_cg(assets_to_prefetch):
                    try:
                        if not hasattr(bot_instance, 'coingecko'):
                            from utils.coingecko import CoinGecko
                            bot_instance.coingecko = CoinGecko(bot_instance.config)
                        for a in assets_to_prefetch:
                            last = coingecko_platforms_cache.get(f"bg_batch:{a}")
                            if last and (time.time() - float(last.get('ts', 0)) < 600):
                                continue
                            coingecko_platforms_cache[f"bg_batch:{a}"] = {'ts': time.time()}
                            try:
                                cg_data = await asyncio.to_thread(
                                    bot_instance.coingecko.get_coingecko_data_for_symbol_sync,
                                    a, None, False
                                )
                                if isinstance(cg_data, dict):
                                    cid = cg_data.get('id') or cg_data.get('coin_id')
                                    plat = cg_data.get('platforms') or {}
                                    platforms_new = {str(k).lower(): (v or '') for k, v in plat.items()} if isinstance(plat, dict) else {}
                                    homepage_val = None
                                    try:
                                        homepages = (cg_data.get('links') or {}).get('homepage') or []
                                        if isinstance(homepages, list):
                                            for cand in homepages:
                                                cand = str(cand or '').strip()
                                                if cand:
                                                    homepage_val = cand
                                                    break
                                    except Exception:
                                        homepage_val = None
                                    image_val = None
                                    try:
                                        img = cg_data.get('image') or {}
                                        if isinstance(img, dict):
                                            image_val = img.get('large') or img.get('small') or img.get('thumb')
                                    except Exception:
                                        image_val = None
                                    entry = {
                                        'ts': time.time(),
                                        'coin_id': cid,
                                        'platforms': platforms_new,
                                        'homepage': homepage_val,
                                        'image': image_val,
                                        'name': cg_data.get('name') or None,
                                    }
                                    coingecko_platforms_cache[a] = entry
                                    if cid:
                                        coingecko_platforms_cache[f"coin:{cid}"] = dict(entry)
                            except Exception as exc:
                                logging.debug(f"[BATCH CG] failed for {a}: {exc}")
                    except Exception as exc:
                        logging.debug(f"[BATCH CG] batch failure: {exc}")

                asyncio.run_coroutine_threadsafe(_bg_prefetch_cg(normalized), bot_instance.loop)
            except Exception:
                pass

        return jsonify({
            "success": True,
            "data": result,
            "requested": normalized,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
        })
    except Exception as e:
        logging.error(f"Критическая ошибка /api/asset_status_batch: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e), "data": {}}), 500


@app.route('/api/asset_status/<path:symbol>')
def get_asset_status(symbol):
    """Возвращает статус депозитов/выводов по монете (по сетям) для поддерживаемых бирж (best-effort, публичные API).

    Базовая структура:
      [{exchange, asset, chain, deposit_enabled, withdraw_enabled, withdraw_fee, min_withdraw}]

    Дополнительно (по запросу ?include_contracts=1):
      - в каждой записи: canonical_chain, contract (best-effort)
      - в payload: token_resolution (platforms/coin_id)
    """
    global bot_instance, asset_status_cache
    try:
        if not bot_instance or not getattr(bot_instance, 'loop', None):
            return jsonify({"success": False, "error": "Бот не инициализирован"}), 400
        if not getattr(bot_instance, 'session', None):
            return jsonify({"success": False, "error": "HTTP-сессия недоступна"}), 400
        if not hasattr(bot_instance, 'calc'):
            return jsonify({"success": False, "error": "Калькулятор не инициализирован"}), 400

        asset = extract_base_asset(symbol, assume_pair=True) if symbol else ''
        if not asset:
            return jsonify({"success": False, "error": "Некорректный символ"}), 400

        # Локальные ссылки, заголовки, кэш
        now = time.time()
        session = bot_instance.session
        default_headers = {
            "User-Agent": "Mozilla/5.0 (asset-status)",
            "Accept": "application/json"
        }
        def is_truthy_param(v) -> bool:
            return str(v or '').strip().lower() in ('1', 'true', 'yes', 'y', 'on')

        def to_bool(v):
            if v is None:
                return None
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                if v == 1:
                    return True
                if v == 0:
                    return False
            s = str(v).strip().lower()
            if s in ('1', 'true', 'yes', 'y', 'on', 'open', 'enabled'):
                return True
            if s in ('0', 'false', 'no', 'n', 'off', 'closed', 'disabled'):
                return False
            return None

        def has_known_status(rows) -> bool:
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                if row.get('deposit_enabled') is not None or row.get('withdraw_enabled') is not None:
                    return True
            return False

        # Позволяем принудительно игнорировать кэш через ?force=1
        force_refresh = str(request.args.get('force', '0')).lower() in ('1', 'true', 'yes')
        include_contracts = is_truthy_param(request.args.get('include_contracts'))
        cached = asset_status_cache.get(asset)
        if (not force_refresh) and cached and (now - cached.get('ts', 0) < ASSET_STATUS_TTL):
            payload = {"success": True, "data": cached.get('data', [])}
            # Контракты кэшируются отдельно; не хотим дергать CG на каждый запрос
            if include_contracts:
                # Always build a token_resolution, even when CoinGecko cache
                # is empty — UI relies on `dex_direct_links` to avoid Google
                # fallback, and those links only need exchange contracts.
                cg_cached = coingecko_platforms_cache.get(asset)
                cg_hit_cached = bool(cg_cached and (now - cg_cached.get('ts', 0) < COINGECKO_PLATFORMS_TTL))
                cg_platforms = (cg_cached.get('platforms') or {}) if cg_hit_cached else {}
                cg_coin_id = cg_cached.get('coin_id') if cg_hit_cached else None
                cg_home = cg_cached.get('homepage') if cg_hit_cached else None
                cg_img = cg_cached.get('image') if cg_hit_cached else None
                cg_name = cg_cached.get('name') if cg_hit_cached else None

                rows_cached = list(cached.get('data') or [])
                # Reuse the same canon/direct-link builders we use on the
                # cold path. We inline small helpers here because we are
                # before the main canon_platform definition in this request.
                def _quick_canon(ch: str) -> str:
                    s = (ch or '').strip().lower()
                    if not s:
                        return ''
                    m = {
                        'eth': 'ethereum', 'erc20': 'ethereum', 'ethereum': 'ethereum',
                        'bsc': 'binance-smart-chain', 'bep20': 'binance-smart-chain',
                        'bep-20': 'binance-smart-chain', 'binance smart chain': 'binance-smart-chain',
                        'bnb smart chain': 'binance-smart-chain', 'bnb chain': 'binance-smart-chain',
                        'polygon': 'polygon-pos', 'matic': 'polygon-pos', 'polygon-pos': 'polygon-pos',
                        'tron': 'tron', 'trc20': 'tron', 'trc-20': 'tron', 'trx': 'tron',
                        'sol': 'solana', 'solana': 'solana',
                        'arbitrum': 'arbitrum-one', 'arbitrum one': 'arbitrum-one',
                        'arbitrumone': 'arbitrum-one', 'arb': 'arbitrum-one', 'arbevm': 'arbitrum-one',
                        'optimism': 'optimistic-ethereum', 'op': 'optimistic-ethereum',
                        'opeth': 'optimistic-ethereum', 'optimistic-ethereum': 'optimistic-ethereum',
                        'base': 'base', 'avalanche': 'avalanche', 'avax': 'avalanche',
                        'c-chain': 'avalanche', 'avax_c': 'avalanche', 'avax-c': 'avalanche',
                        'sui': 'sui', 'sui mainnet': 'sui', 'sui network': 'sui',
                        'aptos': 'aptos', 'apt': 'aptos',
                        'ton': 'the-open-network', 'toncoin': 'the-open-network',
                        'the open network': 'the-open-network', 'the-open-network': 'the-open-network',
                        'near': 'near-protocol', 'near protocol': 'near-protocol',
                    }
                    if s in m:
                        return m[s]
                    if 'bnb' in s or 'bep20' in s or 'bsc' in s:
                        return 'binance-smart-chain'
                    if 'erc20' in s or 'ethereum' in s:
                        return 'ethereum'
                    if 'arbitrum' in s:
                        return 'arbitrum-one'
                    if 'optim' in s:
                        return 'optimistic-ethereum'
                    if 'polygon' in s or 'matic' in s:
                        return 'polygon-pos'
                    if 'tron' in s or 'trc20' in s:
                        return 'tron'
                    return s.split('(', 1)[0].strip() or s
                ex_contracts: Dict[str, str] = {}
                for r in rows_cached:
                    if not isinstance(r, dict):
                        continue
                    canon = _quick_canon(r.get('chain') or '')
                    if not canon or canon in ('-', 'unknown'):
                        continue
                    addr = (r.get('contract_address') or r.get('contract') or '').strip()
                    if not addr or addr.lower() in ('native', 'native coin', '-'):
                        continue
                    ex_contracts.setdefault(canon, addr)

                # Merge CG platforms into missing chains.
                merged_contracts = dict(ex_contracts)
                for p, a in (cg_platforms or {}).items():
                    if p and a and p not in merged_contracts and p not in ('-', 'unknown'):
                        merged_contracts[p] = a

                # Direct DS/GT URL builder (same tables as cold path).
                DS_SLUG = {
                    'ethereum': 'ethereum', 'binance-smart-chain': 'bsc',
                    'polygon-pos': 'polygon', 'tron': 'tron',
                    'solana': 'solana', 'arbitrum-one': 'arbitrum',
                    'optimistic-ethereum': 'optimism', 'base': 'base',
                    'avalanche': 'avalanche', 'sui': 'sui', 'aptos': 'aptos',
                    'the-open-network': 'ton', 'near-protocol': 'near',
                }
                GT_SLUG = {
                    'ethereum': 'eth', 'binance-smart-chain': 'bsc',
                    'polygon-pos': 'polygon_pos', 'tron': 'tron',
                    'solana': 'solana', 'arbitrum-one': 'arbitrum',
                    'optimistic-ethereum': 'optimism', 'base': 'base',
                    'avalanche': 'avax', 'sui': 'sui', 'aptos': 'aptos',
                    'the-open-network': 'ton', 'near-protocol': 'near',
                }
                dex_direct_links: List[Dict[str, str]] = []
                for canon, addr in merged_contracts.items():
                    if not canon or not addr:
                        continue
                    ds_slug = DS_SLUG.get(canon)
                    gt_slug = GT_SLUG.get(canon)
                    dex_direct_links.append({
                        'chain': canon,
                        'contract': addr,
                        'dexscreener': (
                            f"https://dexscreener.com/{ds_slug}/{addr}" if ds_slug
                            else f"https://dexscreener.com/search?q={addr}"
                        ),
                        'geckoterminal': (
                            f"https://www.geckoterminal.com/{gt_slug}/tokens/{addr}" if gt_slug
                            else f"https://www.geckoterminal.com/search?q={addr}"
                        ),
                    })

                payload["token_resolution"] = {
                    "asset": asset,
                    "coin_id": cg_coin_id,
                    "coingecko_url": (
                        f"https://www.coingecko.com/en/coins/{cg_coin_id}" if cg_coin_id
                        else f"https://www.coingecko.com/en/search?query={asset}"
                    ),
                    "homepage": cg_home,
                    "image": cg_img,
                    "name": cg_name,
                    "platforms": cg_platforms,
                    "contracts": merged_contracts,
                    "platforms_map": {},
                    "exchange_contracts": ex_contracts,
                    "dex_direct_links": dex_direct_links,
                    "pending": not cg_hit_cached,
                }
            return jsonify(payload)

        # Если ArbitrageCalculator удален, используем список бирж напрямую из бота
        if bot_instance.calc:
            enabled_exchanges = bot_instance.calc.get_enabled_exchanges()
        else:
            enabled_exchanges = getattr(bot_instance, 'exchanges', [])
        # Фильтруем биржи для добавления плейсхолдеров: только те, где монета действительно листится
        def has_asset_strict(ex) -> bool:
            try:
                pairs = getattr(ex, 'available_pairs', None)
                if isinstance(pairs, set) and len(pairs) > 0:
                    return asset in pairs or asset.upper() in pairs or asset.lower() in pairs
            except Exception:
                pass
            # Если список пар неизвестен/пуст, считаем, что не знаем — плейсхолдер не добавляем
            return False

        eligible_exchanges = [ex for ex in enabled_exchanges if has_asset_strict(ex)]
        # Фетчеры запускаем для всех включённых бирж — даже если пары ещё не подгружены
        enabled_ex_names_all = {ex.name.lower() for ex in enabled_exchanges}

        # --- Фетчеры: предпочтение аутентифицированным (utils.exchange_info), чтобы статусы работали на Bybit/MEXC/CoinEx ---
        # Переводим выход `exchange_info` -> формат /api/asset_status
        def _normalize_exchange_info_row(row: Dict[str, Any]) -> Dict[str, Any]:
            return {
                'exchange': row.get('exchange'),
                'asset': row.get('asset') or asset,
                'chain': row.get('chain') or '-',
                'deposit_enabled': to_bool(row.get('deposit_enabled')),
                'withdraw_enabled': to_bool(row.get('withdraw_enabled')),
                'withdraw_fee': row.get('withdraw_fee'),
                'min_withdraw': row.get('min_withdraw'),
                'contract_address': row.get('contract') or row.get('contract_address') or row.get('tokenAddress')
            }

        async def fetch_okx(asset):
            from utils.exchange_info import exchange_info_fetcher
            rows = await exchange_info_fetcher.get_okx_info(asset)
            return [_normalize_exchange_info_row(r) for r in (rows or [])]

        async def fetch_kucoin(asset):
            from utils.exchange_info import exchange_info_fetcher
            rows = await exchange_info_fetcher.get_kucoin_info(asset)
            return [_normalize_exchange_info_row(r) for r in (rows or [])]

        async def fetch_gateio(asset):
            # Сначала пробуем аутентифицированный эндпоинт (дает сети/цепочки), иначе падаем на публичный fallback.
            try:
                from utils.exchange_info import exchange_info_fetcher
                rows = await exchange_info_fetcher.get_gateio_info(asset)
                norm = [_normalize_exchange_info_row(r) for r in (rows or [])]
                if norm:
                    return norm
            except Exception:
                pass

            # Public fallback (не всегда дает сети)
            url = f"https://api.gateio.ws/api/v4/spot/currencies/{asset}"
            async with session.get(url, headers=default_headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status != 200:
                    return []
                d = await resp.json()
                if not d or 'currency' not in d:
                    return []
                dep_disabled = to_bool(d.get('deposit_disabled'))
                wd_disabled = to_bool(d.get('withdraw_disabled'))
                return [{
                    'exchange': 'Gate.io',
                    'asset': asset,
                    'chain': '-',
                    'deposit_enabled': None if dep_disabled is None else (not dep_disabled),
                    'withdraw_enabled': None if wd_disabled is None else (not wd_disabled),
                    'withdraw_fee': None,
                    'min_withdraw': None,
                    'contract_address': None
                }]

        async def fetch_bitget(asset):
            url = "https://api.bitget.com/api/spot/v1/public/currencies"
            try:
                async with session.get(url, headers=default_headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status != 200:
                        logging.warning(f"[Bitget] asset status failed with code {resp.status}")
                        return []
                    js = await resp.json()
                    data = js.get('data', [])
                    out = []
                    for d in data:
                        if str(d.get('coin', '')).upper() == asset:
                            for ch in d.get('chains', []):
                                out.append({
                                    'exchange': 'Bitget',
                                    'asset': asset,
                                    'chain': ch.get('chain') or '-',
                                    'deposit_enabled': to_bool(ch.get('rechargeable')),
                                    'withdraw_enabled': to_bool(ch.get('withdrawable')),
                                    'withdraw_fee': ch.get('withdrawFee'),
                                    'min_withdraw': ch.get('minWithdrawAmount'),
                                    'contract_address': ch.get('contractAddress') or ch.get('contract') or ch.get('tokenAddress')
                                })
                            return out # Возвращаем как только нашли
                    return []
            except Exception as e:
                logging.error(f"[Bitget] Error fetching asset status for {asset}: {e}")
                return []
        
        async def fetch_bitget_v2(asset):
            try:
                from utils.exchange_info import exchange_info_fetcher
                rows = await exchange_info_fetcher.get_bitget_info(asset)
                return [_normalize_exchange_info_row(r) for r in (rows or [])]
            except Exception as e:
                logging.error(f"[Bitget] Error fetching asset status for {asset}: {e}")
                return []

        async def fetch_mexc(asset):
            from utils.exchange_info import exchange_info_fetcher
            rows = await exchange_info_fetcher.get_mexc_info(asset)
            return [_normalize_exchange_info_row(r) for r in (rows or [])]

        async def fetch_htx(asset):
            # HTX (Huobi) v2 reference API: chain-level statuses
            url = f"https://api.huobi.pro/v2/reference/currency?currency={asset}"
            try:
                async with session.get(url, headers=default_headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status != 200:
                        logging.warning(f"[HTX] asset status failed with code {resp.status} for {asset}")
                        return []
                    js = await resp.json()
                    data = js.get('data') or []
                    out = []
                    for d in data:
                        if str(d.get('currency', '')).upper() != asset:
                            continue
                        for ch in (d.get('chains') or []):
                            dep = ch.get('depositStatus')
                            wd = ch.get('withdrawStatus')
                            dep_b = (str(dep).strip().lower() == 'allowed') if dep is not None else None
                            wd_b = (str(wd).strip().lower() == 'allowed') if wd is not None else None
                            out.append({
                                'exchange': 'HTX',
                                'asset': asset,
                                'chain': ch.get('chain') or ch.get('displayName') or '-',
                                'deposit_enabled': dep_b,
                                'withdraw_enabled': wd_b,
                                'withdraw_fee': ch.get('transactFeeWithdraw') or ch.get('withdrawFee'),
                                'min_withdraw': ch.get('minWithdrawAmt') or ch.get('withdrawMinAmount'),
                                'contract_address': ch.get('contractAddress') or ch.get('contract') or ch.get('tokenAddress')
                            })
                    return out
            except Exception as e:
                logging.error(f"[HTX] Error fetching asset status for {asset}: {e}")
                return []

        async def fetch_bybit(asset):
            from utils.exchange_info import exchange_info_fetcher
            rows = await exchange_info_fetcher.get_bybit_info(asset)
            return [_normalize_exchange_info_row(r) for r in (rows or [])]

        async def fetch_coinex(asset):
            from utils.exchange_info import exchange_info_fetcher
            rows = await exchange_info_fetcher.get_coinex_info(asset)
            return [_normalize_exchange_info_row(r) for r in (rows or [])]

        # NOTE: fetch_tradeogre was removed — TradeOgre is a dead exchange
        # and used to trigger per-request timeouts on the status endpoint.

        async def fetch_nonkyc(asset):
            url = f"https://nonkyc.io/api/v2/currency/{asset}"
            try:
                async with session.get(url, headers=default_headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        d = await resp.json()
                        maint = to_bool(d.get('is_maintenance'))
                        return [{
                            'exchange': 'NonKYC',
                            'asset': asset,
                            'chain': '-',
                            'deposit_enabled': None if maint is None else (not maint),
                            'withdraw_enabled': None if maint is None else (not maint),
                            'withdraw_fee': d.get('fee'),
                            'min_withdraw': d.get('minimum_withdrawal'),
                            'contract_address': None
                        }]
                    return []
            except Exception as e:
                logging.error(f"[NonKYC] Error fetching asset status for {asset}: {e}")
                return []

        async def fetch_safetrade(asset):
            url = "https://safe.trade/api/v2/peatio/public/currencies"
            try:
                async with session.get(url, headers=default_headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for d in data:
                            if str(d.get('id', '')).upper() == asset:
                                return [{
                                    'exchange': 'SafeTrade',
                                    'asset': asset,
                                    'chain': '-',
                                    'deposit_enabled': to_bool(d.get('deposit_enabled')),
                                    'withdraw_enabled': to_bool(d.get('withdrawal_enabled')),
                                    'withdraw_fee': d.get('withdraw_fee'),
                                    'min_withdraw': d.get('min_withdraw_amount'),
                                    'contract_address': None
                                }]
                    return []
            except Exception as e:
                logging.error(f"[SafeTrade] Error fetching asset status for {asset}: {e}")
                return []

        # --- Диспетчер задач ---
        fetcher_map = {
            'okx': fetch_okx,
            'kucoin': fetch_kucoin,
            'gate.io': fetch_gateio,
            'gateio': fetch_gateio,
            'bitget': fetch_bitget_v2,
            'mexc': fetch_mexc,
            'htx': fetch_htx,
            'huobi': fetch_htx,
            'huobi global': fetch_htx,
            'bybit': fetch_bybit,
            'coinex': fetch_coinex,
            'nonkyc': fetch_nonkyc,
            'safetrade': fetch_safetrade
        }

        # Диагностика + ускорение: ограничиваем время на биржу и общий дедлайн.
        errors: Dict[str, str] = {}
        per_exchange_timeout_sec = float(request.args.get('per_exchange_timeout', 4.5) or 4.5)
        overall_timeout_sec = float(request.args.get('overall_timeout', 8) or 8)

        async def run_one(ex_key: str, fetch_fn):
            try:
                return await asyncio.wait_for(fetch_fn(asset), timeout=per_exchange_timeout_sec)
            except asyncio.TimeoutError:
                errors[ex_key] = f"timeout>{per_exchange_timeout_sec}s"
                return []
            except Exception as e:
                errors[ex_key] = str(e)
                return []

        tasks = []
        task_ex_keys = []
        for ex_name_lower in enabled_ex_names_all:
            if ex_name_lower in fetcher_map:
                fetch_fn = fetcher_map[ex_name_lower]
                tasks.append(run_one(ex_name_lower, fetch_fn))
                task_ex_keys.append(ex_name_lower)

        # --- Сбор и обработка результатов ---
        async def gather_all():
            if not tasks:
                return []
            res = await asyncio.gather(*tasks, return_exceptions=True)
            flat = []
            for r in res:
                if isinstance(r, list):
                    flat.extend(r)
                elif isinstance(r, Exception):
                    # Не теряем исключение, но и не валим весь ответ
                    logging.error(f"A fetcher failed during gather: {r}")
            return flat

        # stale-while-revalidate: если есть кэш, он устарел и не запрошено форс-обновление — отдать кэш сразу и обновить фоновой задачей
        if (not force_refresh) and cached and (now - cached.get('ts', 0) >= ASSET_STATUS_TTL):
            try:
                fut_bg = asyncio.run_coroutine_threadsafe(gather_all(), bot_instance.loop)
                def _on_done(f):
                    try:
                        res = f.result()
                        if has_known_status(res):
                            asset_status_cache[asset] = {'ts': time.time(), 'data': res}
                    except Exception as e:
                        logging.error(f"Background refresh failed for asset_status {asset}: {e}")
                fut_bg.add_done_callback(_on_done)
            except Exception as e:
                logging.error(f"Failed to schedule background refresh for asset_status {asset}: {e}")
            return jsonify({"success": True, "data": cached.get('data', [])})

        async def gather_safe():
            try:
                return await gather_all()
            except Exception as e:
                logging.error(f"Gather safe failed: {e}")
                return []

        fut = asyncio.run_coroutine_threadsafe(asyncio.wait_for(gather_safe(), timeout=overall_timeout_sec), bot_instance.loop)
        try:
            data = fut.result(timeout=overall_timeout_sec + 2)
        except Exception as te:
            logging.error(f"/api/asset_status timeout for {asset}: {te}")
            return jsonify({"success": False, "error": "timeout", "data": [], "errors": errors}), 200

        # Если по каким-то причинам ничего не собрали, пробуем компактный быстрый fallback (без ретраев)
        if not data:
            async def fallback_minimal():
                try:
                    res = await asyncio.gather(fetch_kucoin(asset), fetch_gateio(asset), return_exceptions=True)
                    flat = []
                    for r in res:
                        if isinstance(r, list):
                            flat.extend(r)
                    return flat
                except Exception:
                    return []
            try:
                fut_fb = asyncio.run_coroutine_threadsafe(asyncio.wait_for(fallback_minimal(), timeout=6), bot_instance.loop)
                fb = fut_fb.result(timeout=8)
                if fb:
                    data = fb
            except Exception:
                pass

        if errors:
            logging.warning(f"/api/asset_status {asset} fetch errors: {errors}")
        if not data and not errors:
            logging.warning(f"/api/asset_status {asset}: empty data with no fetcher errors (missing API keys or asset not listed).")

        has_known_data = has_known_status(data)
        if (not has_known_data) and cached and not force_refresh:
            payload = {"success": True, "data": cached.get('data', []), "stale": True}
            if errors:
                payload["errors"] = errors
            return jsonify(payload)

        # --- Добавляем "Неизвестно" для остальных ---
        fetched_exchanges = {str(d.get('exchange', '')).lower() for d in data if isinstance(d, dict)}
        # Some exchanges (notably Bybit since late 2023) require API keys
        # for deposit/withdraw/contract endpoints — without keys they
        # return []. Instead of a single dead placeholder "chain = -",
        # synthesize one row per chain that OTHER exchanges have already
        # confirmed for this asset. Contract for each chain is the same
        # because ERC-20 / Solana SPL tokens have ONE canonical address
        # regardless of which CEX lists them. We flag these rows with
        # inferred_from_peers=True so the UI can explain the source.
        # Lightweight canonicalizer — enough to dedupe ETH/ERC20/Ethereum
        # or BSC/BEP20 into a single row when building inferred placeholders.
        # We intentionally duplicate a few entries from the big canon_platform
        # map below because that map is defined further down in this handler.
        def _chain_canon(n: str) -> str:
            s = (n or '').strip().lower()
            if not s:
                return ''
            import re
            m = re.search(r'\b(eth(?:ereum)?|erc(?:20|-20))\b', s)
            if m:
                return 'ethereum'
            m = re.search(r'\b(bsc|bep(?:20|-20)|binance[- ]?smart[- ]?chain|bnb[- ]?chain)\b', s)
            if m:
                return 'binance-smart-chain'
            m = re.search(r'\b(polygon|matic|polygon[- ]?pos)\b', s)
            if m:
                return 'polygon-pos'
            m = re.search(r'\b(tron|trc(?:20|-20))\b', s)
            if m:
                return 'tron'
            m = re.search(r'\b(sol|solana|spl)\b', s)
            if m:
                return 'solana'
            m = re.search(r'\b(arb|arbitrum(?:[- ]one)?)\b', s)
            if m:
                return 'arbitrum-one'
            m = re.search(r'\b(op|optimism|opeth|optimistic[- ]ethereum)\b', s)
            if m:
                return 'optimistic-ethereum'
            m = re.search(r'\b(base)\b', s)
            if m:
                return 'base'
            m = re.search(r'\b(avax|avalanche|c[- ]chain)\b', s)
            if m:
                return 'avalanche'
            m = re.search(r'\b(sui)\b', s)
            if m:
                return 'sui'
            m = re.search(r'\b(apt(?:os)?)\b', s)
            if m:
                return 'aptos'
            return s.split('(', 1)[0].strip()

        peer_chains: Dict[str, Dict[str, Any]] = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            ch = str(row.get('chain') or '').strip()
            if not ch or ch == '-':
                continue
            contract = row.get('contract_address') or row.get('contract')
            if not contract:
                continue
            canon = _chain_canon(ch) or ch.lower()
            if canon not in peer_chains:
                # Prefer a "clean" display name: canonical title-cased over
                # "ERC20" or "Ethereum(ERC20)". Keep whatever the first
                # exchange reported as display but under canonical key.
                display = {
                    'ethereum': 'Ethereum',
                    'binance-smart-chain': 'BNB Smart Chain',
                    'polygon-pos': 'Polygon',
                    'tron': 'Tron',
                    'solana': 'Solana',
                    'arbitrum-one': 'Arbitrum One',
                    'optimistic-ethereum': 'Optimism',
                    'base': 'Base',
                    'avalanche': 'Avalanche',
                    'sui': 'Sui',
                    'aptos': 'Aptos',
                }.get(canon, ch)
                peer_chains[canon] = {'chain': display, 'contract': contract}

        for ex in enabled_exchanges:
            if ex.name.lower() in fetched_exchanges:
                continue
            if peer_chains:
                for info in peer_chains.values():
                    data.append({
                        'exchange': ex.name,
                        'asset': asset,
                        'chain': info['chain'],
                        'deposit_enabled': None,
                        'withdraw_enabled': None,
                        'withdraw_fee': None,
                        'min_withdraw': None,
                        'contract_address': info['contract'],
                        'inferred_from_peers': True,
                    })
            else:
                data.append({
                    'exchange': ex.name,
                    'asset': asset,
                    'chain': '-',
                    'deposit_enabled': None,
                    'withdraw_enabled': None,
                    'withdraw_fee': None,
                    'min_withdraw': None,
                    'contract_address': None,
                })

        def canon_platform(name: str) -> str:
            n = (name or '').strip().lower()
            if not n:
                return ''
            mapping = {
                'eth': 'ethereum', 'erc20': 'ethereum', 'ethereum': 'ethereum',
                'bsc': 'binance-smart-chain', 'binance smart chain': 'binance-smart-chain', 'bep20': 'binance-smart-chain', 'bep-20': 'binance-smart-chain',
                'bnb smart chain': 'binance-smart-chain', 'bnb chain': 'binance-smart-chain', 'bnb': 'binance-smart-chain',
                'polygon': 'polygon-pos', 'matic': 'polygon-pos', 'polygon-pos': 'polygon-pos',
                'tron': 'tron', 'trc20': 'tron', 'trc-20': 'tron', 'trx': 'tron',
                'sol': 'solana', 'solana': 'solana', 'spl': 'solana',
                'arbitrum': 'arbitrum-one', 'arbitrum one': 'arbitrum-one', 'arbitrumone': 'arbitrum-one', 'arb': 'arbitrum-one', 'arbevm': 'arbitrum-one', 'arbi': 'arbitrum-one',
                'optimism': 'optimistic-ethereum', 'op': 'optimistic-ethereum', 'opeth': 'optimistic-ethereum', 'optimistic-ethereum': 'optimistic-ethereum',
                'base': 'base', 'baseevm': 'base',
                'avalanche': 'avalanche', 'avax': 'avalanche', 'c-chain': 'avalanche', 'avax_c': 'avalanche', 'avax-c': 'avalanche', 'avaxc': 'avalanche', 'avaxcchain': 'avalanche',
                'fantom': 'fantom', 'ftm': 'fantom',
                'kcc': 'kucoin-community-chain', 'kucoin community chain': 'kucoin-community-chain',
                'sui': 'sui', 'sui mainnet': 'sui', 'sui network': 'sui',
                'aptos': 'aptos', 'apt': 'aptos', 'aptos mainnet': 'aptos',
                'ton': 'the-open-network', 'toncoin': 'the-open-network',
                'the open network': 'the-open-network', 'the-open-network': 'the-open-network',
                'near': 'near-protocol', 'near protocol': 'near-protocol', 'near-protocol': 'near-protocol',
            }
            if n in mapping:
                return mapping[n]
            # Fallback: strip "(BEP20)", "(ERC20)" style suffix and re-check.
            stripped = n.split('(', 1)[0].strip()
            if stripped and stripped != n and stripped in mapping:
                return mapping[stripped]
            # Substring heuristics for common variations we have not listed.
            if 'bnb' in n or 'bep20' in n or 'bep-20' in n or 'bsc' in n:
                return 'binance-smart-chain'
            if 'erc20' in n or 'erc-20' in n or 'ethereum' in n or n == 'eth':
                return 'ethereum'
            if 'trc20' in n or 'trc-20' in n or 'tron' in n:
                return 'tron'
            if 'arbitrum' in n:
                return 'arbitrum-one'
            if 'optim' in n:
                return 'optimistic-ethereum'
            if 'polygon' in n or 'matic' in n:
                return 'polygon-pos'
            if 'avax' in n or 'avalanche' in n:
                return 'avalanche'
            if 'solana' in n:
                return 'solana'
            return stripped or n

        def build_platforms_map(rows: List[Dict[str, Any]]) -> Dict[str, str]:
            try:
                tmp: Dict[str, set] = {}
                for row in rows:
                    raw = (row.get('chain') or '').strip()
                    canon = canon_platform(raw)
                    if not canon or canon in ('-', 'unknown'):
                        continue
                    tmp.setdefault(canon, set())
                    if raw and raw not in ('-', canon):
                        tmp[canon].add(raw)
                return {k: ", ".join(sorted(v)) for k, v in tmp.items() if v}
            except Exception:
                return {}

        def build_contracts_from_exchange(rows: List[Dict[str, Any]]) -> Dict[str, str]:
            contracts: Dict[str, str] = {}
            try:
                for row in rows:
                    raw = (row.get('chain') or '').strip()
                    canon = canon_platform(raw)
                    if not canon or canon in ('-', 'unknown'):
                        continue
                    addr = (row.get('contract_address') or row.get('contract') or '').strip()
                    if not addr:
                        continue
                    if addr.lower() in ('native', 'native coin', 'native token', '-'):
                        continue
                    if canon not in contracts:
                        contracts[canon] = addr
            except Exception:
                pass
            return contracts

        def attach_row_contracts(rows: List[Dict[str, Any]], platforms: Optional[Dict[str, str]] = None):
            platforms_lc = {str(k).lower(): v for k, v in (platforms or {}).items()}
            for row in rows:
                chain_raw = row.get('chain') or ''
                canonical_chain = canon_platform(chain_raw)
                row['canonical_chain'] = canonical_chain
                contract = row.get('contract_address') or row.get('contract')
                if not contract:
                    contract = platforms_lc.get(str(canonical_chain).lower()) or None
                row['contract'] = contract

        # Optional: enrich results with contracts (CoinGecko platforms)
        token_resolution = None
        if include_contracts:
            try:
                if not hasattr(bot_instance, 'coingecko'):
                    from utils.coingecko import CoinGecko
                    bot_instance.coingecko = CoinGecko(bot_instance.config)

                # Быстро: используем только кэш CoinGecko. Если кэша нет — возвращаем контракты от бирж и обновляем CoinGecko в фоне.
                platforms_map = build_platforms_map(data)
                exchange_contracts = build_contracts_from_exchange(data)

                # Priority: exchange contracts (verified by each CEX's own
                # deposit/withdraw endpoint) are more reliable than
                # CoinGecko platforms (which can be stale or wrong for
                # microcaps and re-branded tokens). So we merge in this
                # order:
                #   1) exchange_contracts  -- primary, from live CEX data
                #   2) CoinGecko platforms -- supplementary, fills chains
                #      that no exchange reported, and provides extra
                #      metadata (coin_id, logo) for the UI.
                platforms: Dict[str, str] = {}
                coin_id = None
                cg_homepage: Optional[str] = None
                cg_image: Optional[str] = None
                cg_name: Optional[str] = None
                cg_cached = coingecko_platforms_cache.get(asset)
                cg_hit = bool(cg_cached and (now - cg_cached.get('ts', 0) < COINGECKO_PLATFORMS_TTL))
                if cg_hit:
                    platforms = cg_cached.get('platforms') or {}
                    coin_id = cg_cached.get('coin_id')
                    cg_homepage = cg_cached.get('homepage')
                    cg_image = cg_cached.get('image')
                    cg_name = cg_cached.get('name')

                contracts: Dict[str, str] = {}
                # 1) Seed from exchange_contracts (primary source).
                for p, addr in (exchange_contracts or {}).items():
                    if p and addr and p not in ('-', 'unknown'):
                        contracts[p] = addr
                # 2) Fill any missing chain from CoinGecko platforms.
                if platforms:
                    try:
                        seen_platforms = {canon_platform((row.get('chain') or '')).lower() for row in data}
                        for p in sorted(seen_platforms):
                            if not p or p in ('-', 'unknown') or p in contracts:
                                continue
                            addr = (platforms.get(p) or '').strip()
                            if addr:
                                contracts[p] = addr
                        # Also include CG chains that no exchange listed
                        # (rare but helpful: e.g. a token bridged to a L2
                        # that only DEXes trade).
                        for p, addr in platforms.items():
                            if p and addr and p not in contracts and p not in ('-', 'unknown'):
                                contracts[p] = addr
                    except Exception:
                        pass

                # Build direct URLs so the frontend never needs Google.
                # Priority:
                #   coingecko_url = coins/{id} when we know it, else search
                #   homepage = CoinGecko-provided homepage[0], else null
                #   dex_links = per-chain direct DexScreener/GeckoTerminal
                #               links from the actual contract address
                coingecko_url: Optional[str] = (
                    f"https://www.coingecko.com/en/coins/{coin_id}" if coin_id
                    else f"https://www.coingecko.com/en/search?query={asset}"
                )
                # Direct (not "search?q=") links per chain. DexScreener uses
                # slugs like "ethereum","bsc","polygon","arbitrum","optimism",
                # "solana","tron","avalanche","base","sui". GeckoTerminal
                # uses a slightly different set ("eth","bsc","polygon_pos",
                # "arbitrum","optimism","solana","tron","avax","base","sui").
                DS_SLUG = {
                    'ethereum': 'ethereum', 'binance-smart-chain': 'bsc',
                    'polygon-pos': 'polygon', 'tron': 'tron',
                    'solana': 'solana', 'arbitrum-one': 'arbitrum',
                    'optimistic-ethereum': 'optimism', 'base': 'base',
                    'avalanche': 'avalanche', 'sui': 'sui', 'aptos': 'aptos',
                    'the-open-network': 'ton', 'near-protocol': 'near',
                    'fantom': 'fantom', 'linea': 'linea',
                    'zksync-era': 'zksync', 'kucoin-community-chain': 'kcc',
                }
                GT_SLUG = {
                    'ethereum': 'eth', 'binance-smart-chain': 'bsc',
                    'polygon-pos': 'polygon_pos', 'tron': 'tron',
                    'solana': 'solana', 'arbitrum-one': 'arbitrum',
                    'optimistic-ethereum': 'optimism', 'base': 'base',
                    'avalanche': 'avax', 'sui': 'sui', 'aptos': 'aptos',
                    'the-open-network': 'ton', 'near-protocol': 'near',
                    'fantom': 'ftm', 'linea': 'linea',
                    'zksync-era': 'zksync-era', 'kucoin-community-chain': 'kcc',
                }
                dex_direct_links: List[Dict[str, str]] = []
                for canon, addr in contracts.items():
                    if not canon or not addr:
                        continue
                    ds_slug = DS_SLUG.get(canon)
                    gt_slug = GT_SLUG.get(canon)
                    entry = {'chain': canon, 'contract': addr}
                    # DexScreener: direct token page when slug known, else search.
                    entry['dexscreener'] = (
                        f"https://dexscreener.com/{ds_slug}/{addr}" if ds_slug
                        else f"https://dexscreener.com/search?q={addr}"
                    )
                    # GeckoTerminal: direct token page when slug known, else search.
                    entry['geckoterminal'] = (
                        f"https://www.geckoterminal.com/{gt_slug}/tokens/{addr}" if gt_slug
                        else f"https://www.geckoterminal.com/search?q={addr}"
                    )
                    dex_direct_links.append(entry)

                token_resolution = {
                    'asset': asset,
                    'coin_id': coin_id,
                    'coingecko_url': coingecko_url,
                    'homepage': cg_homepage or None,
                    'image': cg_image or None,
                    'name': cg_name or None,
                    'platforms': platforms,
                    'contracts': contracts,
                    'platforms_map': platforms_map,
                    'exchange_contracts': dict(exchange_contracts),
                    'dex_direct_links': dex_direct_links,
                    'pending': not cg_hit,
                }
                # Attach the best-known contract into each per-chain row.
                # Start with exchange_contracts so rows show their own
                # bank's contract first; fall back to CG platforms for
                # chains only CG knows about.
                merged_for_rows = dict(exchange_contracts)
                for k, v in (platforms or {}).items():
                    if k and v and k not in merged_for_rows:
                        merged_for_rows[k] = v
                attach_row_contracts(data, merged_for_rows)

                if not cg_hit:

                    async def _bg_refresh_cg():
                        try:
                            cg_data = await asyncio.to_thread(
                                bot_instance.coingecko.get_coingecko_data_for_symbol_sync,
                                asset,
                                None,
                                False
                            )
                            if isinstance(cg_data, dict):
                                cid = cg_data.get('id') or cg_data.get('coin_id')
                                plat = cg_data.get('platforms') or {}
                                platforms_new = {str(k).lower(): (v or '') for k, v in plat.items()} if isinstance(plat, dict) else {}

                                # Extract homepage, image and readable name so
                                # the UI can show direct site/CG links instead
                                # of falling back to a Google search.
                                homepage_val = None
                                try:
                                    links = cg_data.get('links') or {}
                                    homepages = links.get('homepage') or []
                                    if isinstance(homepages, list):
                                        for candidate in homepages:
                                            candidate = str(candidate or '').strip()
                                            if candidate:
                                                homepage_val = candidate
                                                break
                                except Exception:
                                    homepage_val = None
                                image_val = None
                                try:
                                    image = cg_data.get('image') or {}
                                    if isinstance(image, dict):
                                        image_val = image.get('large') or image.get('small') or image.get('thumb')
                                except Exception:
                                    image_val = None
                                name_val = cg_data.get('name') or None

                                cache_entry = {
                                    'ts': time.time(),
                                    'coin_id': cid,
                                    'platforms': platforms_new,
                                    'homepage': homepage_val,
                                    'image': image_val,
                                    'name': name_val,
                                }
                                coingecko_platforms_cache[asset] = cache_entry
                                if cid:
                                    coingecko_platforms_cache[f"coin:{cid}"] = dict(cache_entry)
                        except Exception as e:
                            logging.debug(f"[CG] background refresh failed for {asset}: {e}")

                    try:
                        # Не обновляем чаще раза в 10 минут
                        last = coingecko_platforms_cache.get(f"bg:{asset}")
                        if not last or (time.time() - float(last.get('ts', 0)) > 600):
                            coingecko_platforms_cache[f"bg:{asset}"] = {'ts': time.time()}
                            asyncio.run_coroutine_threadsafe(_bg_refresh_cg(), bot_instance.loop)
                    except Exception:
                        pass
            except Exception as e:
                logging.error(f"Contract enrichment failed for {asset}: {e}")
                token_resolution = {'asset': asset, 'error': str(e)}

        # Optional ERC-20 contract validation via CoinGecko
        validation = None
        try:
            validate_flag = str(request.args.get('validate_contract', '0')).lower() in ('1', 'true', 'yes')
            if validate_flag:
                chain_param = (request.args.get('chain') or 'ethereum').strip()
                input_contract = (request.args.get('contract') or '').strip()
                canonical_chain = canon_platform(chain_param)
                # Ensure CoinGecko client exists
                if not hasattr(bot_instance, 'coingecko'):
                    from utils.coingecko import CoinGecko
                    bot_instance.coingecko = CoinGecko(bot_instance.config)
                cg_data = bot_instance.coingecko.get_coingecko_data_for_symbol_sync(asset, include_tickers=False)
                platforms = {}
                cg_contract = None
                coin_id = None
                if isinstance(cg_data, dict):
                    coin_id = cg_data.get('id') or cg_data.get('coin_id') or None
                    plat = cg_data.get('platforms') or {}
                    # Normalize platform keys to lowercase for matching
                    if isinstance(plat, dict):
                        platforms = {str(k).lower(): v for k, v in plat.items()}
                        cg_contract = platforms.get(canonical_chain)
                input_contract_norm = input_contract.lower() if input_contract else ''
                cg_contract_norm = (cg_contract or '').lower()
                match_status = None
                if input_contract_norm:
                    if cg_contract_norm:
                        match_status = 'match' if input_contract_norm == cg_contract_norm else 'mismatch'
                    else:
                        match_status = 'not_found'
                else:
                    match_status = 'contract_not_provided' if cg_contract_norm else 'not_found'
                validation = {
                    'coin_id': coin_id,
                    'asset': asset,
                    'chain': chain_param,
                    'canonical_chain': canonical_chain,
                    'coingecko_contract': cg_contract,
                    'input_contract': input_contract if input_contract else None,
                    'match_status': match_status,
                    'platforms': platforms
                }
        except Exception as ve:
            logging.error(f"Contract validation error for {asset}: {ve}")
            validation = {'error': str(ve)}
        payload = {"success": True, "data": data}
        if errors:
            payload["errors"] = errors
        if token_resolution is not None:
            payload["token_resolution"] = token_resolution
        if validation is not None:
            payload["validation"] = validation
        if has_known_data:
            asset_status_cache[asset] = {'ts': time.time(), 'data': data}
        return jsonify(payload)
    except Exception as e:
        logging.error(f"Критическая ошибка /api/asset_status/{symbol}: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e), "data": []}), 200

@app.route('/api/full_coin_info/<path:symbol>')
def get_full_coin_info(symbol):
    """
    Унифицированный эндпоинт для получения всех данных о монете за один запрос.
    Объединяет: CG Info, CG Chart, Orderbooks, Asset Status и Arbitrage Opportunities.
    """
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "error": "Бот не инициализирован"}), 200

    # Параметры из запроса (для совместимости с существующими вызовами)
    buy_ex = request.args.get('buy_ex', '')
    sell_ex = request.args.get('sell_ex', '')
    buy_p = request.args.get('buy_p')
    sell_p = request.args.get('sell_p')
    buy_volume = request.args.get('buy_volume')
    sell_volume = request.args.get('sell_volume')
    exchanges_param = request.args.get('exchanges', '')
    context_exchanges: List[str] = []
    try:
        if exchanges_param:
            context_exchanges = [x.strip() for x in str(exchanges_param).split(",") if x.strip()]
    except Exception:
        context_exchanges = []
    if not context_exchanges:
        context_exchanges = [x for x in [buy_ex, sell_ex] if x]

    # 1. Получаем CG Info (через существующую логику, но без jsonify)
    # Т.к. get_coingecko_info возвращает Response, мы вызовем его и распарсим, 
    # либо просто используем логику напрямую. Для надежности вызовем внутренний метод если он есть.
    # В данном случае проще вызвать get_coingecko_info и вытащить данные.
    
    # Но лучше реализовать сбор асинхронно через asyncio.gather для максимальной скорости
    
    async def collect_all_data():
        # Фабрики для асинхронных задач
        
        # 1a. Метод для фетча CG данных (синхронный в боте, но обернем в thread)
        def fetch_cg():
            try:
                # Очистка до базового ассета
                # Если символ похож на контракт, используем его напрямую
                is_contract = symbol.startswith('0x') or len(symbol) > 30
                clean_symbol = symbol if is_contract else extract_base_asset(symbol, assume_pair=True) or str(symbol).upper()

                # Если это контракт, пробуем найти его в CG
                if is_contract:
                    return bot_instance.coingecko.get_coingecko_data_for_symbol_sync(
                        clean_symbol,
                        include_tickers=False
                    )

                # Контекст для выбора правильного coin_id при неоднозначном символе:
                # - список бирж, где мы реально видим эту пару
                # - референс-цена (только для USD-стейбл пар) для price-guard ±25%
                ex_names: List[str] = []
                try:
                    if exchanges_param:
                        ex_names = [x.strip() for x in str(exchanges_param).split(",") if x.strip()]
                except Exception:
                    ex_names = []
                if not ex_names:
                    ex_names = [x for x in [buy_ex, sell_ex] if x]

                ref_price_usd = None
                try:
                    _, quote = split_pair_symbol(symbol)
                    stable_quotes = {"USDT", "USDC", "USD", "BUSD", "TUSD", "FDUSD", "DAI"}
                    if quote and quote.upper() in stable_quotes:
                        bp = float(buy_p) if buy_p is not None else None
                        sp = float(sell_p) if sell_p is not None else None
                        if bp and sp and bp > 0 and sp > 0:
                            ref_price_usd = (bp + sp) / 2.0
                        elif bp and bp > 0:
                            ref_price_usd = bp
                        elif sp and sp > 0:
                            ref_price_usd = sp
                except Exception:
                    ref_price_usd = None

                return bot_instance.coingecko.get_coingecko_data_for_symbol_sync(
                    clean_symbol,
                    None,
                    include_tickers=False,
                    exchange_names=ex_names or None,
                    reference_price_usd=ref_price_usd,
                    price_tolerance=0.25,
                )
            except Exception as e:
                logging.error(f"Error in full_info fetch_cg: {e}")
                return {"error": str(e)}

        # 1b. Метод для фетча графиков
        def fetch_chart():
            try:
                # Базовая цена для уровней
                base = None
                try:
                    if buy_p and sell_p: base = (float(buy_p) + float(sell_p)) / 2.0
                except: pass

                # NOTE: Историю (CoinGecko chart) грузим отдельно на фронте через /api/coingecko_chart,
                # чтобы /api/full_coin_info не зависел от ещё одного CG запроса и не таймаутился.
                prices = []

                # Лестница уровней
                ladder = []
                if base and base > 0:
                    for pct in [3, 5, 10, 20, 50]:
                        ladder.append({"pct": pct, "up": base * (1 + pct/100), "down": base * (1 - pct/100)})

                return {"prices": prices, "ladder": ladder}
            except Exception as e:
                logging.error(f"Error in full_info fetch_chart: {e}")
                return {"success": False, "error": str(e)}

        # 1c. Асинхронный фетч стаканов
        async def fetch_obs():
            try:
                if not bot_instance.calc or not bot_instance.session: return []
                # Для модалки "Инфо": используем список бирж из контекста (если есть), иначе buy/sell
                wanted = [x for x in (context_exchanges or []) if x] or [x for x in [buy_ex, sell_ex] if x]
                res = await bot_instance.calc.fetch_order_books_for(
                    bot_instance.session,
                    [symbol],
                    exchange_names=wanted or None,
                    per_exchange_timeout_sec=6,
                )
                flat = []
                for ex_name, sym_map in (res or {}).items():
                    if symbol in sym_map:
                        ob = sym_map[symbol]
                        bid = ob.get('bid'); ask = ob.get('ask')
                        flat.append({
                            'exchange': ex_name, 'bid': bid, 'ask': ask,
                            'bid_volume': ob.get('bid_volume'), 'ask_volume': ob.get('ask_volume'),
                            'spread_percent': (((ask - bid) / ask) * 100.0) if bid and ask else None
                        })
                flat.sort(key=lambda x: (999 if x.get('spread_percent') is None else x['spread_percent']))
                return flat
            except Exception as e:
                logging.error(f"Error in full_info fetch_obs: {e}")
                return []

        # 1d. Статус ассета (с кэшированием)
        async def fetch_status():
            # Мы можем просто вызвать внутреннюю логику или сделать запрос к себе (но это неэффективно)
            # Лучше использовать кэш или вызвать асинхронные методы напрямую из fetcher_map
            # Для упрощения здесь: вернем пустой список, если нет в кэше, или вытащим из кэша
            asset = extract_base_asset(symbol, assume_pair=True)
            cached = asset_status_cache.get(asset)
            if cached and (time.time() - cached.get('ts', 0) < ASSET_STATUS_TTL):
                return cached.get('data', [])
            # Если нет в кэше - попробуем запустить быстро пару бирж
            return []

        # Запускаем все параллельно
        loop = asyncio.get_event_loop()
        cg_task = loop.run_in_executor(None, fetch_cg)
        chart_task = loop.run_in_executor(None, fetch_chart)
        obs_task = fetch_obs()

        # CG может быть медленным/лимитированным — не блокируем весь ответ.
        cg_info = None
        try:
            cg_info = await asyncio.wait_for(cg_task, timeout=12)
        except Exception:
            cg_info = {"error": "timeout"}

        chart_data, obs_data = await asyncio.gather(chart_task, obs_task)
        return [cg_info, chart_data, obs_data]

    # Выполняем асинхронный сбор в потоке бота (или локально, если loop недоступен)
    try:
        loop = getattr(bot_instance, "loop", None)
        if loop and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(collect_all_data(), loop)
            try:
                cg_info, chart_data, obs_data = fut.result(timeout=25)
            except TimeoutError:
                return jsonify({"success": False, "error": "timeout"}), 200
        else:
            cg_info, chart_data, obs_data = asyncio.run(asyncio.wait_for(collect_all_data(), timeout=25))

        # Make payload lighter for UI: description/tickers are huge and not used in the modal.
        try:
            if isinstance(cg_info, dict):
                cg_info.pop("description", None)
                cg_info.pop("tickers", None)
        except Exception:
            pass
        
        # Находим арбитражные возможности на основе свежих стаканов
        arbitrage_opps = []
        if bot_instance.calc and obs_data:
            # Превращаем flat back в структуру для калькулятора
            symbol_data = {}
            for d in obs_data:
                symbol_data[d['exchange']] = d
            arbitrage_opps = bot_instance.calc._find_opportunities_for_symbol_from_order_books(symbol, symbol_data)

        # Статистика арбитража
        stats = {}
        if arbitrage_opps:
            spreads = [o['spread'] for o in arbitrage_opps]
            stats = {
                "max_spread": max(spreads),
                "avg_spread": sum(spreads) / len(spreads),
                "exchanges_count": len(set(o['buy_exchange'] for o in arbitrage_opps) | set(o['sell_exchange'] for o in arbitrage_opps))
            }

        snapshot_ts = float(getattr(bot_instance, "_last_all_prices_ts", 0.0) or 0.0)
        snapshot_age_sec = int(max(0.0, time.time() - snapshot_ts)) if snapshot_ts > 0 else None
        try:
            stale_after = float(bot_instance.config.get("monitor_interval", 60) or 60) * 2.0
        except Exception:
            stale_after = 120.0

        return jsonify({
            "success": True,
            "symbol": symbol,
            "context": {
                "buy_ex": buy_ex,
                "sell_ex": sell_ex,
                "buy_p": buy_p,
                "sell_p": sell_p,
                "buy_volume": buy_volume,
                "sell_volume": sell_volume,
                "exchanges": context_exchanges,
            },
            "coin_data": cg_info,
            "chart": chart_data,
            "orderbooks": obs_data,
            "arbitrage": {
                "success": True,
                "direct_opportunities": arbitrage_opps,
                "statistics": stats
            },
            "timestamp": time.time(),
            "snapshot_ts": snapshot_ts if snapshot_ts > 0 else None,
            "snapshot_age_sec": snapshot_age_sec,
            "snapshot_stale": bool(snapshot_age_sec is not None and snapshot_age_sec >= stale_after),
        })
    except Exception as e:
        logging.error(f"Критическая ошибка /api/full_coin_info/{symbol}: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 200
    except Exception as e:
        logging.error(f"Критическая ошибка в /api/asset_status/{symbol}: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)})



_BINANCE_ALPHA_LOOKUP_CACHE = {"ts": 0.0, "tokens": []}

def _binance_alpha_tokens_cached(max_age_sec: int = 300):
    """Small synchronous cache for Binance Alpha Token List used only for UI links."""
    now = time.time()
    try:
        if _BINANCE_ALPHA_LOOKUP_CACHE.get("tokens") and (now - float(_BINANCE_ALPHA_LOOKUP_CACHE.get("ts") or 0)) < max_age_sec:
            return list(_BINANCE_ALPHA_LOOKUP_CACHE.get("tokens") or [])
    except Exception:
        pass
    url = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (ARBX Binance Alpha link resolver)",
        "Accept": "application/json,text/plain,*/*",
    })
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    data = payload.get("data") if isinstance(payload, dict) else []
    if isinstance(data, dict):
        for k in ("list", "tokens", "rows"):
            if isinstance(data.get(k), list):
                data = data.get(k)
                break
    tokens = [x for x in (data if isinstance(data, list) else []) if isinstance(x, dict)]
    _BINANCE_ALPHA_LOOKUP_CACHE["ts"] = now
    _BINANCE_ALPHA_LOOKUP_CACHE["tokens"] = tokens
    return tokens

@app.route('/api/binance_alpha_lookup/<path:symbol>')
def binance_alpha_lookup(symbol):
    """Return the best Binance Alpha token match for a UI direct link.

    This is not a trading API. It is only to avoid wrong keyword-search links,
    especially for short tickers like `1` where Binance search can open another token.
    """
    try:
        raw = str(symbol or '').upper().strip()
        base = re.sub(r'[^A-Z0-9]', '', re.sub(r'(USDT|USDC|USD|BUSD|DAI|FDUSD)$', '', raw))
        if not base:
            return jsonify({"success": False, "error": "empty symbol"}), 400
        tokens = _binance_alpha_tokens_cached()
        candidates = []
        for t in tokens:
            sym = str(t.get('symbol') or '').upper().strip()
            if sym != base:
                continue
            def _f(key):
                try:
                    return float(t.get(key) or 0)
                except Exception:
                    return 0.0
            score = _f('liquidity') * 10.0 + _f('volume24h') + _f('marketCap') * 0.001
            candidates.append((score, t))
        if not candidates:
            return jsonify({"success": False, "error": "not found", "symbol": base})
        candidates.sort(key=lambda x: x[0], reverse=True)
        token = dict(candidates[0][1])
        # Keep only fields useful for UI; avoid dumping huge payloads if Binance expands it.
        compact = {k: token.get(k) for k in (
            'alphaId', 'alphaID', 'symbol', 'name', 'chainId', 'chainName', 'contractAddress',
            'price', 'volume24h', 'liquidity', 'marketCap'
        ) if k in token}
        return jsonify({"success": True, "symbol": base, "token": compact, "candidates": len(candidates)})
    except Exception as exc:
        logging.warning("Binance Alpha lookup failed for %s: %s", symbol, exc)
        return jsonify({"success": False, "error": str(exc)}), 200

@app.route('/api/source_info')
def get_source_info():
    """Возвращает информацию о текущем источнике данных"""
    try:
        if not hasattr(app, 'api_manager'):
            return jsonify({
                'success': False,
                'error': 'API менеджер не инициализирован'
            })
        
        source_info = app.api_manager.get_detailed_source_info()
        
        if 'last_failure' in source_info and source_info['last_failure']:
            source_info['last_failure'] = datetime.fromtimestamp(
                source_info['last_failure']
            ).strftime('%Y-%m-%d %H:%M:%S')
            
        return jsonify({
            'success': True,
            'data': source_info
        })
    except Exception as e:
        logging.error(f"Ошибка при получении информации об источнике: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

def get_coin_market_url(coin_id: str) -> str:
    """Возвращает URL for просмотра информации о монете на текущем источнике"""
    return api_manager.get_coin_url(coin_id)

@app.template_filter('coin_url')
def coin_url_filter(coin_id: str) -> str:
    """Фильтр шаблона for получения URL монеты"""
    return get_coin_market_url(coin_id)


def _ws_ui_enabled() -> bool:
    try:
        cfg = getattr(bot_instance, "config", None)
        return bool(cfg and cfg.get("ws_use_for_ui", True))
    except Exception:
        return False


def _ws_base_url() -> str:
    try:
        cfg = getattr(bot_instance, "config", None)
        port_value = cfg.get("ws_server_port", 8090) if cfg else 8090
        port_num = int(float(port_value or 8090))
    except Exception:
        port_num = 8090
    return f"http://127.0.0.1:{port_num}"

def _ws_port_open() -> bool:
    try:
        cfg = getattr(bot_instance, "config", None)
        port_value = cfg.get("ws_server_port", 8090) if cfg else 8090
        port_num = int(float(port_value or 8090))
    except Exception:
        port_num = 8090
    try:
        with socket.create_connection(("127.0.0.1", port_num), timeout=0.35):
            return True
    except Exception:
        return False


def _fetch_ws_json(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    try:
        cfg = getattr(bot_instance, "config", None)
        timeout = float(cfg.get("ws_ui_timeout_sec", 1.5) if cfg else 1.5)
    except Exception:
        timeout = 1.5
    try:
        query = urllib.parse.urlencode(params or {})
        url = f"{_ws_base_url()}{path}"
        if query:
            url = f"{url}?{query}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=max(0.3, timeout)) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logging.debug("WS UI fetch failed for %s: %s", path, exc)
        return None


def _get_ws_health() -> Optional[dict]:
    if not _ws_ui_enabled():
        return None
    data = _fetch_ws_json("/health")
    if isinstance(data, dict) and data.get("success"):
        _get_ws_health._last = data
        _get_ws_health._last_ts = time.time()
        return data

    # The WS server can be healthy while a single health request is slow
    # during orderbook refinement. Keep the last good health briefly so the UI
    # does not flicker to "WS: off" and mislead the user.
    last = getattr(_get_ws_health, "_last", None)
    last_ts = float(getattr(_get_ws_health, "_last_ts", 0.0) or 0.0)
    try:
        cfg = getattr(bot_instance, "config", None)
        cache_ttl = float(cfg.get("ws_health_cache_ttl_sec", 300.0) if cfg else 300.0)
    except Exception:
        cache_ttl = 300.0
    if isinstance(last, dict) and time.time() - last_ts <= max(30.0, cache_ttl):
        cached = dict(last)
        cached["cached"] = True
        cached["cached_age_sec"] = round(time.time() - last_ts, 1)
        return cached
    return None


def _get_ws_opportunities(min_spread: float, limit: int = 250) -> Tuple[List[dict], Optional[dict]]:
    if not _ws_ui_enabled():
        return [], None
    try:
        cfg = getattr(bot_instance, "config", None)
        max_spread = float(cfg.get("max_spread", 77.0) if cfg else 77.0)
        ttl_sec = float(cfg.get("ws_quote_ttl_sec", 10.0) if cfg else 10.0)
        notional = float(cfg.get("arb_min_notional_usd", 50.0) if cfg else 50.0)
        require_liq = bool(
            cfg.get("ui_arb_filter_liquidity", False) or cfg.get("ws_require_top_liquidity", False)
        ) if cfg else False
    except Exception:
        max_spread, ttl_sec, notional, require_liq = 77.0, 10.0, 50.0, False

    payload = _fetch_ws_json(
        "/opportunities",
        {
            "min_spread": min_spread,
            "max_spread": max_spread,
            "ttl_sec": ttl_sec,
            "limit": limit,
            "notional_usd": notional,
            "require_top_liquidity": "1" if require_liq else "0",
        },
    )
    if not isinstance(payload, dict) or not payload.get("success"):
        return [], None
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        return [], None
    health = _get_ws_health()
    return [_normalize_ws_opportunity(row, notional) for row in rows if isinstance(row, dict)], health


def _normalize_ws_opportunity(row: Dict[str, Any], notional: float) -> dict:
    buy_liq = row.get("buy_top_liquidity_usd")
    sell_liq = row.get("sell_top_liquidity_usd")
    min_liq = row.get("min_top_liquidity_usd")
    ts = row.get("timestamp") or time.time()
    return {
        "symbol": str(row.get("symbol") or "").upper(),
        "buy_exchange": row.get("buy_exchange"),
        "sell_exchange": row.get("sell_exchange"),
        "buy_price": float(row.get("buy_price") or 0),
        "sell_price": float(row.get("sell_price") or 0),
        "spread": float(row.get("spread") or 0),
        "source": "websocket",
        "quote_source": "websocket",
        "quote_sources": [row.get("buy_source"), row.get("sell_source")],
        "buy_source": row.get("buy_source"),
        "sell_source": row.get("sell_source"),
        "timestamp": ts,
        "last_update_ts": ts,
        "buy_volume": buy_liq,
        "sell_volume": sell_liq,
        "liquidity_usd": min_liq,
        "min_liquidity_usd": min_liq,
        "depth_buy_liquidity_usd": buy_liq,
        "depth_sell_liquidity_usd": sell_liq,
        "depth_min_liquidity_usd": min_liq,
        "depth_gross_spread_pct": float(row.get("spread") or 0),
        "depth_executable": bool(row.get("top_liquidity_executable")),
        "depth_notional_usd": notional,
        "execution_quality": "manual_signal" if row.get("manual_only") else "ws_bid_ask",
        "manual_only": bool(row.get("manual_only")),
        "execution_mode": row.get("execution_mode") or ("manual_signal" if row.get("manual_only") else "market_data"),
        "note": "MANUAL ONLY: рыночный сигнал, исполнение руками" if row.get("manual_only") else "",
    }


def self_or_rows_merge(primary_rows: List[dict], fallback_rows: List[dict]) -> List[dict]:
    out: List[dict] = []
    seen = set()
    for row in (primary_rows or []):
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("symbol") or "").upper(),
            str(row.get("buy_exchange") or ""),
            str(row.get("sell_exchange") or ""),
        )
        seen.add(key)
        out.append(row)
    for row in (fallback_rows or []):
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("symbol") or "").upper(),
            str(row.get("buy_exchange") or ""),
            str(row.get("sell_exchange") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


@app.route('/api/ws_health')
def get_ws_health_api():
    health = _get_ws_health()
    if health:
        return jsonify({"success": True, "data": health})
    return jsonify({"success": False, "error": "WS server is disabled or unavailable"})


def update_data_thread(bot):
    """Thread for updating data."""
    global current_data, is_running
    while is_running:
        try:
            if bot and hasattr(bot, 'cached_opportunities'):
                # Debug information
                logging.debug(f"Updating data: {len(bot.cached_opportunities) if bot.cached_opportunities else 0} opportunities")
                
                # Копируем данные, чтобы избежать проблем с многопоточностью
                opportunities = bot.cached_opportunities.copy() if bot.cached_opportunities else []
                
                # Обновляем данные только if бот инициализирован
                current_data["opportunities"] = opportunities
                current_data["status"] = bot.status_var.get() if hasattr(bot, 'status_var') else "Unknown"
                current_data["last_update"] = datetime.fromtimestamp(bot.last_update_time).strftime('%H:%M:%S') if hasattr(bot, 'last_update_time') and bot.last_update_time else ""
                current_data["common_pairs"] = len(bot.calc.common_pairs) if hasattr(bot, 'calc') and hasattr(bot.calc, 'common_pairs') else 0
                calc_obj = getattr(bot, 'calc', None)
                if calc_obj and hasattr(calc_obj, 'get_enabled_exchanges'):
                    current_data["enabled_exchanges"] = [ex.name for ex in calc_obj.get_enabled_exchanges()]
                else:
                    current_data["enabled_exchanges"] = []
        except Exception as e:
            logging.error(f"Error in data update thread: {e}\n{traceback.format_exc()}")
        time.sleep(2)  # Update every 2 seconds

def is_port_available(port):
    """Проверяет, доступен ли порт for использования."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', port))
            return True
        except socket.error:
            return False

def find_available_port(start_port=8080, max_attempts=10):
    """
    Находит доступный порт, начиная с указанного.
    
    Args:
        start_port: Начальный порт for проверки
        max_attempts: Максимальное количество попыток
        
    Returns:
        Tuple[int, bool]: Порт and флаг, указывающий, был ли найден доступный порт
    """
    # Список портов for проверки
    ports_to_try = [start_port]  # Сначала пробуем предпочтительный порт
    
    # Добавляем альтернативные порты (выше 1024)
    alt_ports = [8080, 8000, 8888, 5050, 3000, 9000]
    ports_to_try.extend([p for p in alt_ports if p != start_port])
    
    # Добавляем случайные порты in диапазоне 1025-65535, if нужно больше
    import random
    while len(ports_to_try) < max_attempts:
        random_port = random.randint(1025, 65535)
        if random_port not in ports_to_try:
            ports_to_try.append(random_port)
    
    # Проверяем порты
    for port in ports_to_try[:max_attempts]:
        if is_port_available(port):
            return port, True
    
    # Если not нашли доступный порт
    return start_port, False

def run_server(bot, server_port=8080):
    """Запускает веб-сервер."""
    global bot_instance, port, is_running, asset_status_prefetch_thread, exchange_contract_warmup_thread
    bot_instance = bot
    port = server_port
    is_running = True
    
    # Запускаем поток обновления данных
    data_thread = threading.Thread(target=update_data_thread, args=(bot,), daemon=True)
    data_thread.start()
    # Фоновое обновление статусов ввод/вывод по активам
    try:
        if asset_status_prefetch_thread is None or not asset_status_prefetch_thread.is_alive():
            asset_status_prefetch_thread = threading.Thread(
                target=_asset_status_prefetch_loop,
                args=(bot,),
                daemon=True
            )
            asset_status_prefetch_thread.start()
    except Exception as e:
        logging.error(f"Failed to start asset_status prefetch thread: {e}")
    # Медленный фоновый прогрев полного кэша контрактов/сетей
    try:
        if exchange_contract_warmup_thread is None or not exchange_contract_warmup_thread.is_alive():
            exchange_contract_warmup_thread = threading.Thread(
                target=_exchange_contract_warmup_loop,
                args=(bot,),
                daemon=True
            )
            exchange_contract_warmup_thread.start()
    except Exception as e:
        logging.error(f"Failed to start exchange contract warmup thread: {e}")
    
    # Запускаем сервер
    try:
        try:
            from waitress import serve
            logging.info("Запуск веб-сервера через Waitress (WSGI)")
            serve(app, host=os.getenv('WEB_HOST', '127.0.0.1'), port=port)
        except ImportError:
            logging.info("Waitress не установлена, используем встроенный Flask сервер")
            app.run(host=os.getenv('WEB_HOST', '127.0.0.1'), port=port, debug=False, use_reloader=False)
    except Exception as e:
        logging.error(f"Ошибка при запуске веб-сервера: {e}\n{traceback.format_exc()}")
        is_running = False
        return False
    return True

def start_web_server(bot, server_port=8080, open_browser=True):
    """
    Запускает веб-сервер in отдельном потоке.
    
    Args:
        bot: Экземпляр ArbitrageBot
        server_port: Порт for веб-сервера
        open_browser: Открывать ли браузер автоматически
        
    Returns:
        bool: Успешность запуска
    """
    global server_thread, port
    
    # Проверяем, установлен ли Flask
    try:
        import flask
    except ImportError:
        logging.error("Flask not установлен. Веб-интерфейс not будет доступен.")
        logging.error("Установите Flask: pip install flask flask-cors")
        return False
    
    # Создаем директории for шаблонов and статических файлов, if их нет
    os.makedirs(os.path.join(os.path.dirname(__file__), 'templates'), exist_ok=True)
    os.makedirs(os.path.join(os.path.dirname(__file__), 'static'), exist_ok=True)
    
    # Проверяем доступность порта and ищем альтернативный, if нужно
    available_port, success = find_available_port(server_port)
    if not success:
        logging.error(f"Не удалось найти доступный порт. Попытка запуска на порту {available_port}, но это может not сработать.")
        print(f"ОШИБКА: Не удалось найти доступный порт. Попытка запуска на порту {available_port}, но это может not сработать.")
    elif available_port != server_port:
        logging.warning(f"Порт {server_port} занят. Используем альтернативный порт {available_port}.")
        print(f"ВНИМАНИЕ: Порт {server_port} занят. Используем альтернативный порт {available_port}.")
    
    port = available_port
    
    # Запускаем сервер in отдельном потоке
    server_thread = threading.Thread(target=run_server, args=(bot, available_port), daemon=True)
    server_thread.start()
    
    global kraken_kyber_scan_thread
    kraken_kyber_scan_thread = threading.Thread(target=_kraken_kyber_background_loop, daemon=True, name="kraken-kyber-background")
    kraken_kyber_scan_thread.start()
    
    logging.info(f"Веб-сервер запущен на порту {available_port}")
    
    # Открываем браузер
    if open_browser:
        # Даем серверу время на запуск
        time.sleep(1)
        webbrowser.open(f"http://localhost:{available_port}")
        logging.info("Браузер открыт")
    
    return True

def stop_web_server():
    """Останавливает веб-сервер."""
    global is_running
    is_running = False
    logging.info("Веб-сервер остановлен")




def _kraken_kyber_background_loop():
    global bot_instance, is_running, kraken_kyber_scan_cache_live
    logging.info("Started Kraken↔Kyber continuous background scanner loop")
    while True:
        if not is_running: return
        if not bot_instance or not getattr(bot_instance, "running", False):
            time.sleep(2)
            continue
        break
    
    # Reload config per run if needed, but scanner instance is fine
    while is_running:
        try:
            if not bot_instance or not getattr(bot_instance, "running", False):
                time.sleep(5)
                continue
            
            scanner = KrakenKyberScanner(bot_instance.config)
            min_spread = float(bot_instance.config.get('kraken_kyber_min_spread', 0.5) or 0.5)
            max_spread = float(bot_instance.config.get('max_spread', 100.0) or 100.0)
            asset_limit = int(bot_instance.config.get('kraken_kyber_asset_limit', 1000) or 1000)
            notional_usd = float(bot_instance.config.get('kraken_kyber_notional_usd', 250.0) or 250.0)

            payload = asyncio.run(scanner.scan(
                min_spread=min_spread,
                max_spread=max_spread,
                limit=500,
                asset_limit=asset_limit,
                notional_usd=notional_usd
            ))
            kraken_kyber_scan_cache_live["payload"] = payload
            kraken_kyber_scan_cache_live["ts"] = time.time()
            
            # Sleep 5 seconds between full passes to save bandwidth
            time.sleep(5)
        except Exception:
            logging.exception("Background Kraken↔Kyber scanner failed")
            time.sleep(10)

def _start_kraken_kyber_index_refresh(force: bool = False) -> bool:
    """Start slow Kraken→CoinGecko→contracts index refresh in background."""
    global kraken_kyber_index_thread, bot_instance
    try:
        with kraken_kyber_index_lock:
            if kraken_kyber_index_thread and kraken_kyber_index_thread.is_alive():
                return False
            if not bot_instance:
                return False
            scanner = KrakenKyberScanner(bot_instance.config)
            if not force and not scanner.contract_index_needs_refresh():
                return False
            def _run():
                try:
                    asyncio.run(scanner.build_contract_index(force=force))
                except Exception:
                    logging.exception("Kraken↔Kyber contract index refresh failed")
            kraken_kyber_index_thread = threading.Thread(target=_run, daemon=True, name="kraken-kyber-indexer")
            kraken_kyber_index_thread.start()
            return True
    except Exception:
        logging.exception("Cannot start Kraken↔Kyber index refresh")
        return False


@app.route('/api/kraken_kyber_index_status')
def api_kraken_kyber_index_status():
    status = get_kraken_kyber_index_status()
    return jsonify({"success": True, "status": status})


@app.route('/api/kraken_kyber_refresh_index', methods=['POST'])
def api_kraken_kyber_refresh_index():
    force = str(request.args.get('force', '0')).strip().lower() in ('1', 'true', 'yes', 'on')
    started = _start_kraken_kyber_index_refresh(force=force)
    return jsonify({"success": True, "started": started, "status": get_kraken_kyber_index_status()})

@app.route('/api/kraken_kyber_opportunities')
def get_kraken_kyber_opportunities():
    """Separate scanner: Kraken Pro ↔ KyberSwap.

    Runs only when the UI opens the Kraken↔Kyber tab.  It does not change or
    pollute the classic CEX↔CEX scanner.
    """
    global bot_instance, kraken_kyber_scan_cache_live
    if not bot_instance:
        return jsonify({"success": False, "error": "Bot not initialized"}), 200
    try:
        payload = kraken_kyber_scan_cache_live.get("payload")
        if payload is None:
            # First scan not finished yet
            payload = {
                "success": True,
                "scanner": "kraken_kyber",
                "data": [],
                "stats": {"pairs_with_contracts": 0, "kyber_quotes": 0, "routes_raw": 0},
                "meta": {"contract_index": get_kraken_kyber_index_status()}
            }
        
        # We still apply the min_spread / max_spread filter from the UI request
        try:
            min_spread = float(request.args.get('min_spread', bot_instance.config.get('kraken_kyber_min_spread', 0.5)) or 0)
        except Exception:
            min_spread = 0.0
        try:
            max_spread = float(request.args.get('max_spread', bot_instance.config.get('max_spread', 100.0)) or 100.0)
        except Exception:
            max_spread = 100.0

        filtered_data = []
        for row in payload.get("data", []):
            spr = float(row.get("spread", 0))
            if min_spread <= spr <= max_spread:
                filtered_data.append(row)
        
        ret = dict(payload)
        ret["data"] = filtered_data
        
        return jsonify(ret), 200
    except Exception as exc:
        logging.exception("Kraken↔Kyber scanner cache read failed")
        return jsonify({"success": False, "error": str(exc), "scanner": "kraken_kyber"}), 200

@app.route('/api/interchain_opportunities')
def get_interchain_opportunities():
    """Cross-venue scanner for CEX<->DEX and estimated DEX<->DEX bridge routes."""
    global bot_instance, asset_status_cache, dex_quote_cache, interchain_scan_cache
    if not bot_instance:
        return jsonify({"success": False, "error": "Bot not initialized"})

    # Contract-first handler now covers all route groups: cex_dex, cross_chain and all.
    # The scanner inside ContractFirstInterchainScanner.scan() respects self.route_group
    # and returns both CEX<->DEX and cross-chain DEX->bridge->DEX routes (via Mayan and
    # its fallbacks). The legacy scanner below is kept as dead code for emergency
    # fallback only and can be re-enabled by passing &legacy_interchain=1.
    route_group_req = str(request.args.get('route_group', 'all') or 'all').strip().lower()
    use_legacy = str(request.args.get('legacy_interchain', '0')).strip().lower() in ('1', 'true', 'yes', 'on')
    # Read TTL from config (fallback to module-level default)
    _scan_ttl = INTERCHAIN_SCAN_TTL
    try:
        if bot_instance and hasattr(bot_instance, 'config'):
            _scan_ttl = int(bot_instance.config.get("cex_dex_scan_ttl_sec", INTERCHAIN_SCAN_TTL) or INTERCHAIN_SCAN_TTL)
    except Exception:
        pass
    if not use_legacy and route_group_req in ('cex_dex', 'cross_chain', 'all'):
        payload = handle_contract_first_interchain_opportunities(
            bot_instance,
            request.args,
            asset_status_cache,
            dex_quote_cache,
            interchain_scan_cache,
            asset_status_ttl=ASSET_STATUS_TTL,
            dex_quote_ttl=DEX_QUOTE_TTL,
            interchain_scan_ttl=_scan_ttl,
        )
        return jsonify(payload), 200

    # Legacy scanner path (only reached when ?legacy_interchain=1 is passed)

    try:
        min_spread = float(request.args.get('min_spread', 0) or 0)
    except Exception:
        min_spread = 0.0

    try:
        limit = int(float(request.args.get('limit', 80) or 80))
    except Exception:
        limit = 80
    limit = max(10, min(limit, 300))

    try:
        asset_limit = int(float(request.args.get('asset_limit', 40) or 40))
    except Exception:
        asset_limit = 40
    asset_limit = max(5, min(asset_limit, 150))

    requested_assets_raw = str(request.args.get('assets', '') or '').strip()
    requested_assets = []
    if requested_assets_raw:
        seen_requested = set()
        for part in requested_assets_raw.split(','):
            asset = extract_base_asset(part.strip().upper(), assume_pair=True) or str(part or '').strip().upper()
            if asset and asset not in seen_requested:
                seen_requested.add(asset)
                requested_assets.append(asset)
        requested_assets = requested_assets[:30]

    quick_mode = str(request.args.get('quick_mode', '0')).strip().lower() in ('1', 'true', 'yes', 'on')
    live_bridge_quotes = str(request.args.get('live_bridge_quotes', '1')).strip().lower() not in ('0', 'false', 'no', 'off')
    live_only = str(request.args.get('live_only', '0')).strip().lower() in ('1', 'true', 'yes', 'on')
    route_group = str(request.args.get('route_group', 'all') or 'all').strip().lower()
    if route_group not in ('all', 'cex_dex', 'cross_chain'):
        route_group = 'all'
    chain_scope = str(request.args.get('chain_scope', 'all') or 'all').strip().lower()
    if chain_scope not in ('all', 'major', 'small'):
        chain_scope = 'all'
    asset_profile = str(request.args.get('asset_profile', 'balanced') or 'balanced').strip().lower()
    if asset_profile not in ('balanced', 'long_tail', 'majors'):
        asset_profile = 'balanced'
    execution_quality_filter = str(request.args.get('execution_quality', '') or '').strip().lower()
    if live_only and not execution_quality_filter:
        execution_quality_filter = "live"
    if execution_quality_filter not in ("", "estimated", "hybrid", "live", "actionable"):
        execution_quality_filter = ""

    default_notional = bot_instance.config.get("arb_min_notional_usd", 100.0)
    try:
        notional_usd = float(request.args.get('notional_usd', default_notional) or default_notional)
    except Exception:
        notional_usd = float(default_notional or 100.0)
    notional_usd = max(10.0, min(notional_usd, 5000.0))

    snapshot = getattr(bot_instance, "_last_all_prices", None)
    snapshot_ts = float(getattr(bot_instance, "_last_all_prices_ts", 0.0) or 0.0)
    if not isinstance(snapshot, dict) or not snapshot:
        return jsonify({
            "success": False,
            "error": "No ticker snapshot available yet. Wait for the next monitor iteration.",
        }), 200

    enabled_exchange_objs = []
    enabled_exchanges = []
    try:
        calc_obj = getattr(bot_instance, "calc", None)
        if calc_obj and hasattr(calc_obj, "get_enabled_exchanges"):
            enabled_exchange_objs = list(calc_obj.get_enabled_exchanges())
            enabled_exchanges = [ex.name for ex in enabled_exchange_objs]
        elif calc_obj and hasattr(calc_obj, "exchanges"):
            enabled_exchange_objs = [ex for ex in calc_obj.exchanges if getattr(ex, "enabled", False)]
            enabled_exchanges = [ex.name for ex in enabled_exchange_objs]
    except Exception:
        enabled_exchange_objs = []
        enabled_exchanges = []
    if not enabled_exchanges:
        enabled_exchanges = [name for name, prices in snapshot.items() if isinstance(prices, dict)]

    requested_assets_key = ",".join(requested_assets)
    cache_key = (
        f"{snapshot_ts:.3f}:{min_spread:.4f}:{limit}:{asset_limit}:{notional_usd:.2f}:"
        f"{int(quick_mode)}:{int(live_bridge_quotes)}:{route_group}:{chain_scope}:{asset_profile}:{execution_quality_filter or '-'}:{requested_assets_key}"
    )
    cached_scan = interchain_scan_cache.get(cache_key)
    if cached_scan and (time.time() - cached_scan.get("ts", 0) < INTERCHAIN_SCAN_TTL):
        return jsonify(cached_scan.get("payload") or {"success": True, "data": []})

    async def collect_interchain_data():
        major_interchain_chains = {
            "ethereum",
            "binance-smart-chain",
            "polygon-pos",
            "arbitrum-one",
            "optimistic-ethereum",
            "base",
            "solana",
            "avalanche",
            "tron",
            "sui",
            "mantle",
            "linea",
            "scroll",
        }

        def to_float(value: Any) -> Optional[float]:
            try:
                if value in (None, "", "null"):
                    return None
                return float(value)
            except Exception:
                return None

        def canon_chain(name: str) -> str:
            return canon_chain_name(name)

        def chain_matches_scope(chain_name: str) -> bool:
            chain_id = canon_chain(chain_name)
            if not chain_id or chain_id in ("unknown", "-"):
                return chain_scope != "small"
            is_major = chain_id in major_interchain_chains
            if chain_scope == "major":
                return is_major
            if chain_scope == "small":
                return not is_major
            return True

        def route_matches_scope(route: Dict[str, Any]) -> bool:
            chains = {
                canon_chain(route.get("chain")),
                canon_chain(route.get("buy_chain")),
                canon_chain(route.get("sell_chain")),
            }
            chains = {item for item in chains if item and item not in ("unknown", "-")}
            if not chains:
                return chain_scope != "small"
            if chain_scope == "major":
                return all(item in major_interchain_chains for item in chains)
            if chain_scope == "small":
                return any(item not in major_interchain_chains for item in chains)
            return True

        def exchange_key(name: str) -> str:
            return "".join(ch for ch in str(name or "").lower() if ch.isalnum())

        def gas_buffer_usd(chain: str, *, bridge: bool = False) -> float:
            base = {
                "solana": 0.35,
                "tron": 0.75,
                "binance-smart-chain": 0.60,
                "polygon-pos": 0.40,
                "ethereum": 7.50,
                "arbitrum-one": 0.75,
                "optimistic-ethereum": 0.80,
                "base": 0.55,
                "avalanche": 0.90,
                "fantom": 0.75,
                "sui": 0.35,
                "aptos": 0.35,
                "the-open-network": 0.90,
                "near-protocol": 0.60,
            }.get(chain, 1.50)
            return base + (1.50 if bridge else 0.0)

        def transfer_state_label(value: Optional[bool], *, bridge_required: bool = False) -> str:
            if bridge_required:
                return "bridge_required"
            if value is True:
                return "ok"
            if value is False:
                return "blocked"
            return "unknown"

        def stable_price_band(asset: str) -> Optional[Tuple[float, float]]:
            asset_u = str(asset or "").strip().upper()
            if asset_u in {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USD1"}:
                return (0.85, 1.15)
            return None

        def stable_prices_ok(asset: str, *prices: float) -> bool:
            band = stable_price_band(asset)
            if not band:
                return True
            low, high = band
            for price in prices:
                try:
                    value = float(price)
                except Exception:
                    continue
                if value <= 0:
                    continue
                if value < low or value > high:
                    return False
            return True

        def exchange_fee_rate(exchange_name: str) -> float:
            try:
                return max(0.0, float(bot_instance.config.get_exchange_fee(exchange_name, 0.1))) / 100.0
            except Exception:
                return 0.001

        def combine_execution_quality(*modes: Any) -> str:
            normalized = []
            for mode in modes:
                item = str(mode or "").strip().lower()
                if item in {"estimated", "hybrid", "live"}:
                    normalized.append(item)
            if normalized and all(item == "live" for item in normalized):
                return "live"
            if "live" in normalized or "hybrid" in normalized:
                return "hybrid"
            return "estimated"

        def cross_chain_identity_ok(buy_quote: Dict[str, Any], sell_quote: Dict[str, Any]) -> bool:
            if buy_quote.get("chain") == sell_quote.get("chain"):
                return True
            asset_u = str(asset or "").strip().upper()
            if asset_u in {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USD1"}:
                if str(buy_quote.get("contract") or "").strip() and str(sell_quote.get("contract") or "").strip():
                    return True
            trusted_sources = {"coingecko", "jupiter", "mayan"}
            buy_identity = str(buy_quote.get("identity_key") or "").strip().lower()
            sell_identity = str(sell_quote.get("identity_key") or "").strip().lower()
            if buy_identity and sell_identity:
                return buy_identity == sell_identity
            buy_family = str(buy_quote.get("family_key") or "").strip().lower()
            sell_family = str(sell_quote.get("family_key") or "").strip().lower()
            buy_source = str(buy_quote.get("contract_source") or "").strip().lower()
            sell_source = str(sell_quote.get("contract_source") or "").strip().lower()
            if buy_family and sell_family and buy_family == sell_family:
                if buy_family.startswith(("identity:", "origin:", "cg:")):
                    return True
                if (buy_quote.get("verified") and sell_quote.get("verified")) or (
                    buy_source in trusted_sources and sell_source in trusted_sources
                ):
                    return True
            buy_origin_chain = str(buy_quote.get("origin_chain") or "").strip().lower()
            sell_origin_chain = str(sell_quote.get("origin_chain") or "").strip().lower()
            buy_origin_contract = str(buy_quote.get("origin_contract") or "").strip().lower()
            sell_origin_contract = str(sell_quote.get("origin_contract") or "").strip().lower()
            if buy_origin_chain and sell_origin_chain and buy_origin_contract and sell_origin_contract:
                return buy_origin_chain == sell_origin_chain and buy_origin_contract == sell_origin_contract
            buy_cg = str(buy_quote.get("coingecko_id") or "").strip().lower()
            sell_cg = str(sell_quote.get("coingecko_id") or "").strip().lower()
            if buy_cg and sell_cg and buy_cg == sell_cg and (buy_quote.get("verified") or sell_quote.get("verified")):
                return True
            if buy_source not in trusted_sources or sell_source not in trusted_sources:
                return False
            chains = {str(buy_quote.get("chain") or ""), str(sell_quote.get("chain") or "")}
            if "solana" in chains:
                return "jupiter" in {buy_source, sell_source}
            return buy_source == "coingecko" and sell_source == "coingecko"

        preferred_bridge_providers = bot_instance.config.get("interchain_bridge_provider_priority", []) or []
        bridge_blacklist = bot_instance.config.get("interchain_bridge_provider_blacklist", []) or []
        preferred_live_bridge_providers = bot_instance.config.get("interchain_live_bridge_provider_priority", []) or ["mayan", "wormhole", "layerzero", "relay", "debridge"]
        live_bridge_blacklist = bot_instance.config.get("interchain_live_bridge_provider_blacklist", []) or []
        major_quote_assets = {
            "BTC", "WBTC", "ETH", "WETH", "SOL", "BNB", "XRP", "DOGE", "TRX", "TON",
            "ADA", "AVAX", "MATIC", "LINK", "LTC", "BCH", "ATOM", "DOT", "NEAR",
            "APT", "SUI", "FIL", "ARB", "OP", "UNI", "AAVE", "INJ", "XLM",
            "USDC", "USDT", "DAI", "FDUSD", "TUSD", "USD1", "USDE",
        }
        live_bridge_blacklist_ids = {str(item).strip().lower() for item in live_bridge_blacklist if str(item or "").strip()}
        live_bridge_provider_ids = []
        if live_bridge_quotes:
            for provider in preferred_live_bridge_providers:
                provider_id = str(provider or "").strip().lower()
                if provider_id and provider_id not in live_bridge_blacklist_ids and provider_id not in live_bridge_provider_ids:
                    live_bridge_provider_ids.append(provider_id)
            if not live_bridge_provider_ids:
                for provider_id in ("mayan", "wormhole", "layerzero", "relay", "debridge"):
                    if provider_id not in live_bridge_blacklist_ids:
                        live_bridge_provider_ids.append(provider_id)
        asset_status_timeout_sec = 1.4 if quick_mode else 2.5
        max_cross_chain_quotes = 2 if quick_mode else 4
        bridge_gross_profit_floor_usd = 1.0 if quick_mode else 0.0

        # Per-asset timeout (seconds). Default increased for more reliable discovery.
        # Can be overridden via request param `per_asset_timeout` (0.5 - 30.0 seconds).
        per_asset_timeout_override = None
        per_asset_timeout_param = request.args.get('per_asset_timeout', None)
        if per_asset_timeout_param:
            try:
                v = float(per_asset_timeout_param)
                per_asset_timeout_override = max(0.5, min(v, 30.0))
            except Exception:
                per_asset_timeout_override = None

        per_asset_timeout_sec = 6.0 if quick_mode else 12.0
        if per_asset_timeout_override is not None:
            per_asset_timeout_sec = per_asset_timeout_override
        logging.debug("Interchain per-asset timeout set to %.1fs (quick_mode=%s)", per_asset_timeout_sec, quick_mode)

        async def get_asset_rows(asset: str) -> List[Dict[str, Any]]:
            now = time.time()
            cached = asset_status_cache.get(asset)
            if cached and (now - cached.get("ts", 0) < ASSET_STATUS_TTL):
                data = cached.get("data") or []
                return list(data) if isinstance(data, list) else []
            try:
                data = await asyncio.wait_for(
                    _prefetch_asset_status_async(asset, enabled_exchange_objs),
                    timeout=asset_status_timeout_sec,
                )
                if data:
                    asset_status_cache[asset] = {"ts": time.time(), "data": data}
                return list(data or [])
            except Exception as exc:
                logging.debug(f"interchain asset status fetch failed for {asset}: {exc}")
                return []

        async def merge_contracts(asset: str, rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
            contracts: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                chain = canon_chain(row.get("chain"))
                contract = str(
                    row.get("contract_address")
                    or row.get("contract")
                    or ""
                ).strip()
                if not contract or contract.lower() in ("-", "native", "native coin", "native token"):
                    continue
                if chain not in contracts:
                    contracts[chain] = {
                        "contract": contract,
                        "source": "exchange_info",
                    }

            cached_cg = coingecko_platforms_cache.get(asset)
            cg_coin_id = None
            cg_family_key = None
            if isinstance(cached_cg, dict):
                cg_coin_id = str(cached_cg.get("coin_id") or "").strip() or None
                cg_family_key = f"cg:{cg_coin_id.lower()}" if cg_coin_id else None
            if cached_cg and (time.time() - cached_cg.get("ts", 0) < COINGECKO_PLATFORMS_TTL):
                platforms = cached_cg.get("platforms") or {}
                if isinstance(platforms, dict):
                    for chain_raw, contract in platforms.items():
                        contract_str = str(contract or "").strip()
                        if not contract_str:
                            continue
                        chain = canon_chain(chain_raw)
                        current = contracts.get(chain)
                        incoming = {
                            "contract": contract_str,
                            "source": "coingecko",
                            "coingecko_id": cg_coin_id,
                            "family_key": cg_family_key,
                        }
                        if not current:
                            contracts[chain] = incoming
                            continue
                        current_source = str(current.get("source") or "").strip().lower()
                        if contract_str and current_source not in {"mayan", "jupiter"}:
                            current["contract"] = contract_str
                            current["source"] = "coingecko"
                        if cg_coin_id and not current.get("coingecko_id"):
                            current["coingecko_id"] = cg_coin_id
                        if cg_family_key and not current.get("family_key"):
                            current["family_key"] = cg_family_key

            preferred_contracts = [
                str((meta or {}).get("contract") or "").strip()
                for meta in contracts.values()
                if str((meta or {}).get("contract") or "").strip()
            ]
            try:
                mayan_tokens = await resolve_mayan_asset_tokens(
                    session,
                    asset,
                    preferred_contracts=preferred_contracts,
                )
            except Exception as exc:
                logging.debug("Mayan token registry merge failed for %s: %s", asset, exc)
                mayan_tokens = []

            for token in mayan_tokens:
                chain = canon_chain(token.get("chain"))
                if not chain:
                    continue
                incoming = {
                    "contract": str(token.get("contract") or "").strip(),
                    "source": "mayan",
                    "decimals": int(token.get("decimals") or 0) if token.get("decimals") is not None else None,
                    "verified": bool(token.get("verified")),
                    "identity_key": str(token.get("identity_key") or "").strip() or None,
                    "family_key": str(token.get("family_key") or "").strip() or None,
                    "origin_chain": str(token.get("origin_chain") or "").strip() or None,
                    "origin_contract": str(token.get("origin_contract") or "").strip() or None,
                    "coingecko_id": str(token.get("coingecko_id") or "").strip() or None,
                }
                current = contracts.get(chain)
                if not current:
                    contracts[chain] = incoming
                    continue
                current_source = str(current.get("source") or "").strip().lower()
                if current_source == "coingecko" and incoming.get("contract"):
                    current["contract"] = incoming["contract"]
                    current["source"] = "mayan"
                if not current.get("decimals") and incoming.get("decimals"):
                    current["decimals"] = incoming["decimals"]
                if incoming.get("verified"):
                    current["verified"] = True
                for key in ("identity_key", "family_key", "origin_chain", "origin_contract", "coingecko_id"):
                    if not current.get(key) and incoming.get(key):
                        current[key] = incoming[key]

            need_symbol_discovery = chain_scope == "small" or asset_profile == "long_tail" or len(contracts) <= 1
            if need_symbol_discovery:
                try:
                    discovered_tokens = await discover_symbol_contracts(
                        session,
                        asset,
                        preferred_contracts=preferred_contracts,
                    )
                except Exception as exc:
                    logging.debug("symbol discovery merge failed for %s: %s", asset, exc)
                    discovered_tokens = []
                for token in discovered_tokens:
                    chain = canon_chain(token.get("chain"))
                    if not chain:
                        continue
                    incoming = {
                        "contract": str(token.get("contract") or "").strip(),
                        "source": str(token.get("source") or "unknown").strip().lower() or "unknown",
                        "decimals": int(token.get("decimals") or 0) if token.get("decimals") is not None else None,
                        "verified": bool(token.get("verified")),
                        "identity_key": str(token.get("identity_key") or "").strip() or None,
                        "family_key": str(token.get("family_key") or "").strip() or None,
                        "origin_chain": str(token.get("origin_chain") or "").strip() or None,
                        "origin_contract": str(token.get("origin_contract") or "").strip() or None,
                        "coingecko_id": str(token.get("coingecko_id") or "").strip() or None,
                    }
                    current = contracts.get(chain)
                    if not current:
                        contracts[chain] = incoming
                        continue
                    for key in ("decimals", "identity_key", "family_key", "origin_chain", "origin_contract", "coingecko_id"):
                        if not current.get(key) and incoming.get(key):
                            current[key] = incoming[key]
            return contracts

        def trim_contracts(contracts: Dict[str, Dict[str, Any]], asset_name: str) -> Dict[str, Dict[str, Any]]:
            if not contracts:
                return {}

            max_contracts = 10 if quick_mode else 18
            if chain_scope == "small":
                max_contracts = 8 if quick_mode else 14
            if str(asset_name or "").strip().upper() in {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USD1"}:
                max_contracts += 2

            ranked_items = []
            for chain, meta in contracts.items():
                chain_id = canon_chain(chain)
                source = str((meta or {}).get("source") or "unknown").strip().lower()
                is_major = chain_id in major_interchain_chains
                if chain_scope == "major":
                    scope_rank = 0 if is_major else 1
                elif chain_scope == "small":
                    scope_rank = 0 if not is_major else 1
                else:
                    scope_rank = 0
                source_rank = 0 if source == "exchange_info" else 1 if source == "jupiter" else 2 if source == "mayan" else 3 if source == "geckoterminal_search" else 4 if source == "dexscreener_search" else 5 if source == "coingecko" else 6
                solana_rank = 0 if chain_id == "solana" else 1
                ranked_items.append((scope_rank, source_rank, solana_rank, chain_id, chain, meta))

            ranked_items.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
            trimmed: Dict[str, Dict[str, Any]] = {}
            for _, _, _, chain_id, chain, meta in ranked_items:
                if chain_id in trimmed:
                    continue
                trimmed[chain] = meta
                if len(trimmed) >= max_contracts:
                    break
            return trimmed

        def pick_cex_quotes(asset: str) -> List[Dict[str, Any]]:
            quote_priority = {"USDT": 0, "USDC": 1, "USD": 2}
            quotes: List[Dict[str, Any]] = []
            for exchange_name in enabled_exchanges:
                ex_map = snapshot.get(exchange_name) or {}
                if not isinstance(ex_map, dict):
                    continue
                best = None
                for symbol, raw_price in ex_map.items():
                    price = to_float(raw_price)
                    if price is None or price <= 0:
                        continue
                    base, quote = split_pair_symbol(symbol)
                    if base != asset or quote not in quote_priority:
                        continue
                    candidate = {
                        "exchange": exchange_name,
                        "symbol": symbol,
                        "price": float(price),
                        "quote": quote,
                        "priority": quote_priority[quote],
                        "fee_rate": exchange_fee_rate(exchange_name),
                    }
                    if best is None or candidate["priority"] < best["priority"]:
                        best = candidate
                if best:
                    quotes.append(best)
            return quotes

        def find_transfer_row(rows: List[Dict[str, Any]], exchange_name: str, chain: str) -> Optional[Dict[str, Any]]:
            ex_key = exchange_key(exchange_name)
            exact = None
            fallback = None
            for row in rows:
                if exchange_key(row.get("exchange")) != ex_key:
                    continue
                row_chain = canon_chain(row.get("chain"))
                if row_chain == chain:
                    exact = row
                    break
                if fallback is None and row_chain in ("", "-", "unknown"):
                    fallback = row
            return exact or fallback

        def parse_dex_pairs(asset: str, expected_chain: str, contract: str, payload: Any, *, contract_source: str) -> List[Dict[str, Any]]:
            best_by_chain: Dict[str, Dict[str, Any]] = {}
            contract_lc = str(contract or "").strip().lower()
            pairs = payload.get("pairs") if isinstance(payload, dict) else []
            for pair in pairs or []:
                if not isinstance(pair, dict):
                    continue
                base_token = pair.get("baseToken") or {}
                if str(base_token.get("address") or "").strip().lower() != contract_lc:
                    continue
                price = to_float(pair.get("priceUsd"))
                if price is None or price <= 0:
                    continue
                chain_id = canon_chain(pair.get("chainId"))
                if expected_chain and chain_id != expected_chain:
                    continue
                liquidity_usd = to_float((pair.get("liquidity") or {}).get("usd")) or 0.0
                volume_24h = to_float((pair.get("volume") or {}).get("h24")) or 0.0
                score = (liquidity_usd * 10.0) + volume_24h
                item = {
                    "asset": asset,
                    "chain": chain_id,
                    "contract": contract,
                    "contract_source": contract_source,
                    "price": float(price),
                    "liquidity_usd": float(liquidity_usd),
                    "volume_24h": float(volume_24h),
                    "dex_id": str(pair.get("dexId") or "dex").strip() or "dex",
                    "pair_address": str(pair.get("pairAddress") or "").strip(),
                    "url": f"https://dexscreener.com/{pair.get('chainId')}/{pair.get('pairAddress')}",
                    "label": f"{str(pair.get('dexId') or 'DEX').strip()} ({chain_id})",
                    "score": float(score),
                    "quote_source": "dexscreener",
                    "quote_mode": "estimated",
                    "route_labels": [],
                }
                current = best_by_chain.get(chain_id)
                if current is None or item["score"] > current["score"]:
                    best_by_chain[chain_id] = item
            return list(best_by_chain.values())

        async def fetch_dex_quotes(
            session: aiohttp.ClientSession,
            asset: str,
            contracts: Dict[str, Dict[str, Any]],
            solana_token: Optional[Dict[str, Any]] = None,
        ) -> List[Dict[str, Any]]:
            quotes: List[Dict[str, Any]] = []

            if solana_token and solana_token.get("contract"):
                jupiter_quote = await build_jupiter_dex_quote(
                    session,
                    symbol=asset,
                    mint=solana_token.get("contract"),
                    decimals=solana_token.get("decimals"),
                    notional_usd=notional_usd,
                    usd_hint=solana_token.get("price"),
                    metadata=solana_token,
                )
                if jupiter_quote:
                    solana_meta = contracts.get("solana") or {}
                    for field in ("identity_key", "family_key", "origin_chain", "origin_contract", "coingecko_id", "decimals"):
                        if solana_meta.get(field) not in (None, "", [], {}):
                            jupiter_quote[field] = solana_meta.get(field)
                    if solana_meta.get("verified"):
                        jupiter_quote["verified"] = True
                    quotes.append(jupiter_quote)

            for chain, contract_meta in contracts.items():
                contract_str = str((contract_meta or {}).get("contract") or "").strip()
                contract_source = str((contract_meta or {}).get("source") or "unknown").strip().lower() or "unknown"
                if not contract_str:
                    continue
                if not chain_matches_scope(chain):
                    continue
                contract_meta_overlay = {}
                for field in ("identity_key", "family_key", "origin_chain", "origin_contract", "coingecko_id", "decimals"):
                    if (contract_meta or {}).get(field) not in (None, "", [], {}):
                        contract_meta_overlay[field] = (contract_meta or {}).get(field)
                if (contract_meta or {}).get("verified"):
                    contract_meta_overlay["verified"] = True
                quote_cache_key = f"{chain}:{contract_str.lower()}"
                cached_quote = dex_quote_cache.get(quote_cache_key)
                payload = None
                if cached_quote and (time.time() - cached_quote.get("ts", 0) < DEX_QUOTE_TTL):
                    payload = cached_quote.get("data")
                if payload is None:
                    url = f"https://api.dexscreener.com/latest/dex/tokens/{contract_str}"
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                            if response.status == 200:
                                payload = await response.json(content_type=None)
                                dex_quote_cache[quote_cache_key] = {"ts": time.time(), "data": payload}
                    except Exception as exc:
                        logging.debug(f"DexScreener quote fetch failed for {asset} {contract_str}: {exc}")
                dexscreener_quotes = parse_dex_pairs(asset, chain, contract_str, payload, contract_source=contract_source) if payload is not None else []
                if dexscreener_quotes and contract_meta_overlay:
                    for item in dexscreener_quotes:
                        item.update(contract_meta_overlay)
                if dexscreener_quotes:
                    quotes.extend(dexscreener_quotes)
                else:
                    try:
                        gecko_quotes = await fetch_geckoterminal_dex_quotes(
                            session,
                            asset=asset,
                            chain=chain,
                            contract=contract_str,
                            contract_source=contract_source,
                        )
                        if gecko_quotes and contract_meta_overlay:
                            for item in gecko_quotes:
                                item.update(contract_meta_overlay)
                        quotes.extend(gecko_quotes)
                    except Exception as exc:
                        logging.debug(f"GeckoTerminal quote fetch failed for {asset} {chain} {contract_str}: {exc}")

            best_quotes: Dict[str, Dict[str, Any]] = {}
            for quote in quotes:
                current = best_quotes.get(quote["chain"])
                quote_rank = 1 if str(quote.get("quote_mode") or "") == "live" else 0
                current_rank = 1 if current and str(current.get("quote_mode") or "") == "live" else 0
                if current is None or (quote_rank, quote["score"]) > (current_rank, current["score"]):
                    best_quotes[quote["chain"]] = quote
            return list(best_quotes.values())

        def build_cex_to_dex(asset: str, cex_quote: Dict[str, Any], dex_quote: Dict[str, Any], transfer_row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            cex_price = float(cex_quote["price"])
            dex_price = float(dex_quote["price"])
            if cex_price <= 0 or dex_price <= 0:
                return None
            if not stable_prices_ok(asset, cex_price, dex_price):
                return None
            withdraw_enabled = transfer_row.get("withdraw_enabled") if isinstance(transfer_row, dict) else None
            if withdraw_enabled is False:
                return None

            withdraw_fee_asset = to_float(transfer_row.get("withdraw_fee")) if isinstance(transfer_row, dict) else 0.0
            withdraw_fee_asset = max(0.0, withdraw_fee_asset or 0.0)
            dex_fee_rate = 0.003
            gas_usd = gas_buffer_usd(dex_quote["chain"])

            gross_tokens = notional_usd / cex_price
            tokens_after_trade = gross_tokens * (1.0 - float(cex_quote["fee_rate"]))
            tokens_after_transfer = tokens_after_trade - withdraw_fee_asset
            if tokens_after_transfer <= 0:
                return None

            proceeds_usd = tokens_after_transfer * dex_price * (1.0 - dex_fee_rate)
            spread = ((dex_price / cex_price) - 1.0) * 100.0
            net_profit_usd = proceeds_usd - notional_usd - gas_usd
            roi_pct = (net_profit_usd / notional_usd) * 100.0
            if spread < min_spread or net_profit_usd <= 0:
                return None

            return {
                "symbol": f"{asset}USDT",
                "asset": asset,
                "route_kind": "cex_to_dex",
                "buy_type": "cex",
                "sell_type": "dex",
                "buy_exchange": cex_quote["exchange"],
                "sell_exchange": dex_quote["label"],
                "buy_price": cex_price,
                "sell_price": dex_price,
                "spread": spread,
                "net_profit_usd": net_profit_usd,
                "roi_pct": roi_pct,
                "notional_usd": notional_usd,
                "buy_chain": dex_quote["chain"],
                "sell_chain": dex_quote["chain"],
                "chain": dex_quote["chain"],
                "contract": dex_quote["contract"],
                "buy_contract": None,
                "sell_contract": dex_quote["contract"],
                "liquidity_usd": dex_quote["liquidity_usd"],
                "volume_24h": dex_quote["volume_24h"],
                "transfer_status": transfer_state_label(withdraw_enabled),
                "withdraw_fee_asset": withdraw_fee_asset,
                "gas_estimate_usd": gas_usd,
                "bridge_required": False,
                "execution_quality": "live" if str(dex_quote.get("quote_mode") or "") == "live" else "estimated",
                "quote_sources": [str(dex_quote.get("quote_source") or "dex")],
                "buy_url": None,
                "sell_url": dex_quote.get("swap_url") or dex_quote["url"],
                "notes": f"Buy on CEX, withdraw on selected chain, sell on DEX. DEX quote source: {dex_quote.get('quote_source', 'dex')}.",
            }

        def build_dex_to_cex(asset: str, dex_quote: Dict[str, Any], cex_quote: Dict[str, Any], transfer_row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            dex_price = float(dex_quote["price"])
            cex_price = float(cex_quote["price"])
            if cex_price <= 0 or dex_price <= 0:
                return None
            if not stable_prices_ok(asset, cex_price, dex_price):
                return None
            deposit_enabled = transfer_row.get("deposit_enabled") if isinstance(transfer_row, dict) else None
            if deposit_enabled is False:
                return None

            dex_fee_rate = 0.003
            gas_usd = gas_buffer_usd(dex_quote["chain"])
            tokens_after_swap = (notional_usd / dex_price) * (1.0 - dex_fee_rate)
            if tokens_after_swap <= 0:
                return None
            proceeds_usd = tokens_after_swap * cex_price * (1.0 - float(cex_quote["fee_rate"]))
            spread = ((cex_price / dex_price) - 1.0) * 100.0
            net_profit_usd = proceeds_usd - notional_usd - gas_usd
            roi_pct = (net_profit_usd / notional_usd) * 100.0
            if spread < min_spread or net_profit_usd <= 0:
                return None

            return {
                "symbol": f"{asset}USDT",
                "asset": asset,
                "route_kind": "dex_to_cex",
                "buy_type": "dex",
                "sell_type": "cex",
                "buy_exchange": dex_quote["label"],
                "sell_exchange": cex_quote["exchange"],
                "buy_price": dex_price,
                "sell_price": cex_price,
                "spread": spread,
                "net_profit_usd": net_profit_usd,
                "roi_pct": roi_pct,
                "notional_usd": notional_usd,
                "buy_chain": dex_quote["chain"],
                "sell_chain": dex_quote["chain"],
                "chain": dex_quote["chain"],
                "contract": dex_quote["contract"],
                "buy_contract": dex_quote["contract"],
                "sell_contract": None,
                "liquidity_usd": dex_quote["liquidity_usd"],
                "volume_24h": dex_quote["volume_24h"],
                "transfer_status": transfer_state_label(deposit_enabled),
                "withdraw_fee_asset": 0.0,
                "gas_estimate_usd": gas_usd,
                "bridge_required": False,
                "execution_quality": "live" if str(dex_quote.get("quote_mode") or "") == "live" else "estimated",
                "quote_sources": [str(dex_quote.get("quote_source") or "dex")],
                "buy_url": dex_quote.get("swap_url") or dex_quote["url"],
                "sell_url": None,
                "notes": f"Buy on DEX, deposit to CEX on the same chain, sell on CEX. DEX quote source: {dex_quote.get('quote_source', 'dex')}.",
            }

        async def build_dex_bridge_dex(
            session: aiohttp.ClientSession,
            asset: str,
            buy_quote: Dict[str, Any],
            sell_quote: Dict[str, Any],
        ) -> Optional[Dict[str, Any]]:
            buy_price = float(buy_quote["price"])
            sell_price = float(sell_quote["price"])
            if buy_price <= 0 or sell_price <= 0 or buy_quote["chain"] == sell_quote["chain"]:
                return None
            if not cross_chain_identity_ok(buy_quote, sell_quote):
                return None
            if not stable_prices_ok(asset, buy_price, sell_price):
                return None

            bridge_candidates = get_bridge_candidates(
                asset,
                buy_quote["chain"],
                sell_quote["chain"],
                preferred=preferred_bridge_providers,
                blacklist=bridge_blacklist,
            )
            if not bridge_candidates:
                return None
            top_bridge = bridge_candidates[0]

            dex_fee_rate = 0.003
            tokens_after_buy = (notional_usd / buy_price) * (1.0 - dex_fee_rate)
            if tokens_after_buy <= 0:
                return None
            spread = ((sell_price / buy_price) - 1.0) * 100.0
            if spread < min_spread or spread <= 0:
                return None
            chains_label = f"{buy_quote['chain']} -> {sell_quote['chain']}"
            gross_bridge_edge_usd = max(0.0, (sell_price - buy_price) * tokens_after_buy)
            bridge_candidates_payload = [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "docs_url": item.get("docs_url"),
                    "score": item.get("score"),
                }
                for item in bridge_candidates[:4]
            ]

            def build_live_bridge_route(bridge_quote: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                provider_name = str(bridge_quote.get("provider_name") or bridge_quote.get("provider_id") or "bridge").strip()
                provider_id = str(bridge_quote.get("provider_id") or provider_name).strip().lower()
                dest_decimals = int((((bridge_quote.get("dest_token") or {}).get("decimals")) or 0))
                if dest_decimals <= 0:
                    return None
                bridged_tokens = float(bridge_quote.get("amount_out_atomic") or 0) / float(10 ** dest_decimals)
                if bridged_tokens <= 0:
                    return None
                proceeds_usd = bridged_tokens * sell_price * (1.0 - dex_fee_rate)
                wallet_gas_usd = float(bridge_quote.get("wallet_gas_usd") or 0.0)
                bridge_cost_usd = gas_buffer_usd(buy_quote["chain"]) + gas_buffer_usd(sell_quote["chain"]) + wallet_gas_usd
                net_profit_usd = proceeds_usd - notional_usd - bridge_cost_usd
                roi_pct = (net_profit_usd / notional_usd) * 100.0
                if spread < min_spread or net_profit_usd <= 0:
                    return None
                execution_quality = combine_execution_quality(
                    buy_quote.get("quote_mode"),
                    "live",
                    sell_quote.get("quote_mode"),
                )
                return {
                    "symbol": f"{asset}USDT",
                    "asset": asset,
                    "route_kind": "dex_bridge_dex_live",
                    "buy_type": "dex",
                    "sell_type": "dex",
                    "buy_exchange": buy_quote["label"],
                    "sell_exchange": sell_quote["label"],
                    "buy_price": buy_price,
                    "sell_price": sell_price,
                    "spread": spread,
                    "net_profit_usd": net_profit_usd,
                    "roi_pct": roi_pct,
                    "notional_usd": notional_usd,
                    "buy_chain": buy_quote["chain"],
                    "sell_chain": sell_quote["chain"],
                    "chain": chains_label,
                    "contract": buy_quote["contract"],
                    "buy_contract": buy_quote["contract"],
                    "sell_contract": sell_quote["contract"],
                    "liquidity_usd": min(buy_quote["liquidity_usd"], sell_quote["liquidity_usd"]),
                    "volume_24h": min(buy_quote["volume_24h"], sell_quote["volume_24h"]),
                    "transfer_status": transfer_state_label(None, bridge_required=True),
                    "withdraw_fee_asset": 0.0,
                    "gas_estimate_usd": bridge_cost_usd,
                    "bridge_required": True,
                    "bridge_provider": provider_name,
                    "bridge_docs_url": bridge_quote.get("docs_url"),
                    "bridge_candidates": bridge_candidates_payload,
                    "execution_quality": execution_quality,
                    "quote_sources": [
                        str(buy_quote.get("quote_source") or "dex"),
                        provider_id,
                        str(sell_quote.get("quote_source") or "dex"),
                    ],
                    "bridge_quote_mode": "live",
                    "bridge_time_estimate_sec": int(bridge_quote.get("time_estimate_sec") or 0),
                    "bridge_fee_usd": float(bridge_quote.get("relayer_fee_usd") or 0.0),
                    "bridge_min_out_atomic": int(bridge_quote.get("minimum_out_atomic") or 0),
                    "bridge_router": str(bridge_quote.get("router") or provider_id).strip() or provider_id,
                    "buy_url": buy_quote.get("swap_url") or buy_quote["url"],
                    "sell_url": sell_quote.get("swap_url") or sell_quote["url"],
                    "notes": (
                        f"Live asset bridge quote via {provider_name}. "
                        f"Bridge {tokens_after_buy:.6f} {asset} from {buy_quote['chain']} to {sell_quote['chain']} "
                        f"then sell on destination DEX."
                    ),
                }

            def build_inventory_route(rebalance_quote: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                provider_name = str(rebalance_quote.get("provider_name") or rebalance_quote.get("provider_id") or "bridge").strip()
                provider_id = str(rebalance_quote.get("provider_id") or provider_name).strip().lower()
                rebalance_out_usd = float(rebalance_quote.get("amount_out_usd") or 0.0)
                if rebalance_out_usd <= 0:
                    return None
                wallet_gas_usd = float(rebalance_quote.get("wallet_gas_usd") or 0.0)
                bridge_cost_usd = gas_buffer_usd(buy_quote["chain"]) + gas_buffer_usd(sell_quote["chain"]) + wallet_gas_usd
                net_profit_usd = rebalance_out_usd - notional_usd - bridge_cost_usd
                roi_pct = (net_profit_usd / notional_usd) * 100.0
                if spread < min_spread or net_profit_usd <= 0:
                    return None
                rebalance_symbol = str((((rebalance_quote.get("source_token") or {}).get("symbol")) or "USDC")).strip().upper()
                execution_quality = combine_execution_quality(
                    buy_quote.get("quote_mode"),
                    "live",
                    sell_quote.get("quote_mode"),
                )
                return {
                    "symbol": f"{asset}USDT",
                    "asset": asset,
                    "route_kind": "dex_inventory_rebalance",
                    "buy_type": "dex",
                    "sell_type": "dex",
                    "buy_exchange": buy_quote["label"],
                    "sell_exchange": sell_quote["label"],
                    "buy_price": buy_price,
                    "sell_price": sell_price,
                    "spread": spread,
                    "net_profit_usd": net_profit_usd,
                    "roi_pct": roi_pct,
                    "notional_usd": notional_usd,
                    "buy_chain": buy_quote["chain"],
                    "sell_chain": sell_quote["chain"],
                    "chain": chains_label,
                    "contract": buy_quote["contract"],
                    "buy_contract": buy_quote["contract"],
                    "sell_contract": sell_quote["contract"],
                    "liquidity_usd": min(buy_quote["liquidity_usd"], sell_quote["liquidity_usd"]),
                    "volume_24h": min(buy_quote["volume_24h"], sell_quote["volume_24h"]),
                    "transfer_status": "inventory_required",
                    "withdraw_fee_asset": 0.0,
                    "gas_estimate_usd": bridge_cost_usd,
                    "bridge_required": True,
                    "bridge_provider": provider_name,
                    "bridge_docs_url": rebalance_quote.get("docs_url"),
                    "bridge_candidates": bridge_candidates_payload,
                    "execution_quality": execution_quality,
                    "quote_sources": [
                        str(buy_quote.get("quote_source") or "dex"),
                        str(sell_quote.get("quote_source") or "dex"),
                        provider_id,
                    ],
                    "bridge_quote_mode": "rebalance_live",
                    "bridge_time_estimate_sec": int(rebalance_quote.get("time_estimate_sec") or 0),
                    "bridge_fee_usd": float(rebalance_quote.get("relayer_fee_usd") or 0.0),
                    "rebalance_asset": rebalance_symbol,
                    "rebalance_out_usd": rebalance_out_usd,
                    "bridge_router": str(rebalance_quote.get("router") or provider_id).strip() or provider_id,
                    "buy_url": buy_quote.get("swap_url") or buy_quote["url"],
                    "sell_url": sell_quote.get("swap_url") or sell_quote["url"],
                    "notes": (
                        f"Requires token inventory on {sell_quote['chain']}. "
                        f"Net PnL includes live {rebalance_symbol} rebalance quote via {provider_name} back to {buy_quote['chain']}."
                    ),
                }

            if live_bridge_provider_ids and gross_bridge_edge_usd >= bridge_gross_profit_floor_usd:
                relay_chains = await fetch_relay_chains(session) if "relay" in live_bridge_provider_ids else []
                direct_live_routes = []
                for provider_id in live_bridge_provider_ids:
                    bridge_quote = None
                    if provider_id == "relay":
                        source_token = get_relay_featured_token(
                            relay_chains,
                            buy_quote["chain"],
                            asset,
                            contract=buy_quote.get("contract"),
                        )
                        dest_token = get_relay_featured_token(
                            relay_chains,
                            sell_quote["chain"],
                            asset,
                            contract=sell_quote.get("contract"),
                            preferred_symbols=[str((source_token or {}).get("symbol") or asset).strip().upper()],
                        )
                        source_decimals = int((source_token or {}).get("decimals") or 0)
                        dest_decimals = int((dest_token or {}).get("decimals") or source_decimals or 0)
                        if source_token and dest_token and source_decimals > 0 and dest_decimals > 0:
                            bridge_amount_atomic = max(1, int(round(tokens_after_buy * (10 ** source_decimals))))
                            bridge_quote = await fetch_relay_quote(
                                session,
                                source_chain=buy_quote["chain"],
                                dest_chain=sell_quote["chain"],
                                source_token=source_token,
                                dest_token=dest_token,
                                amount_atomic=bridge_amount_atomic,
                            )
                    elif provider_id == "mayan":
                        source_token = await get_mayan_supported_token(
                            session,
                            buy_quote["chain"],
                            asset,
                            contract=buy_quote.get("contract"),
                        )
                        dest_token = await get_mayan_supported_token(
                            session,
                            sell_quote["chain"],
                            asset,
                            contract=sell_quote.get("contract"),
                            preferred_symbols=[str((source_token or {}).get("symbol") or asset).strip().upper()],
                        )
                        source_decimals = int((source_token or {}).get("decimals") or 0)
                        dest_decimals = int((dest_token or {}).get("decimals") or source_decimals or 0)
                        if source_token and dest_token and source_decimals > 0 and dest_decimals > 0:
                            bridge_amount_atomic = max(1, int(round(tokens_after_buy * (10 ** source_decimals))))
                            bridge_quote = await fetch_mayan_quote(
                                session,
                                source_chain=buy_quote["chain"],
                                dest_chain=sell_quote["chain"],
                                source_token=source_token,
                                dest_token=dest_token,
                                amount_atomic=bridge_amount_atomic,
                            )
                    elif provider_id == "wormhole":
                        source_contract = str(buy_quote.get("contract") or "").strip()
                        dest_contract = str(sell_quote.get("contract") or "").strip()
                        if source_contract and dest_contract:
                            source_decimals = int(buy_quote.get("decimals") or 6)
                            bridge_amount_atomic = max(1, int(round(tokens_after_buy * (10 ** source_decimals))))
                            bridge_quote = await fetch_wormhole_quote(
                                session,
                                source_chain=buy_quote["chain"],
                                dest_chain=sell_quote["chain"],
                                source_token={
                                    "address": source_contract,
                                    "contract": source_contract,
                                    "decimals": source_decimals,
                                    "symbol": asset,
                                },
                                dest_token={
                                    "address": dest_contract,
                                    "contract": dest_contract,
                                    "decimals": int(sell_quote.get("decimals") or 6),
                                    "symbol": asset,
                                },
                                amount_atomic=bridge_amount_atomic,
                            )
                    elif provider_id == "layerzero":
                        source_contract = str(buy_quote.get("contract") or "").strip()
                        dest_contract = str(sell_quote.get("contract") or "").strip()
                        source_decimals = int(buy_quote.get("decimals") or 0)
                        dest_decimals = int(sell_quote.get("decimals") or 0)
                        if source_contract and dest_contract and source_decimals > 0 and dest_decimals > 0:
                            bridge_amount_atomic = max(1, int(round(tokens_after_buy * (10 ** source_decimals))))
                            bridge_quote = await fetch_layerzero_quote(
                                session,
                                source_chain=buy_quote["chain"],
                                dest_chain=sell_quote["chain"],
                                source_token={
                                    "address": source_contract,
                                    "contract": source_contract,
                                    "decimals": source_decimals,
                                    "symbol": asset,
                                },
                                dest_token={
                                    "address": dest_contract,
                                    "contract": dest_contract,
                                    "decimals": dest_decimals,
                                    "symbol": asset,
                                },
                                amount_atomic=bridge_amount_atomic,
                            )
                    elif provider_id == "debridge":
                        source_decimals = int(buy_quote.get("decimals") or 0)
                        dest_decimals = int(sell_quote.get("decimals") or 0)
                        source_contract = str(buy_quote.get("contract") or "").strip()
                        dest_contract = str(sell_quote.get("contract") or "").strip()
                        if source_contract and dest_contract and source_decimals > 0 and dest_decimals > 0:
                            bridge_amount_atomic = max(1, int(round(tokens_after_buy * (10 ** source_decimals))))
                            bridge_quote = await fetch_debridge_quote(
                                session,
                                source_chain=buy_quote["chain"],
                                dest_chain=sell_quote["chain"],
                                source_token={
                                    "address": source_contract,
                                    "contract": source_contract,
                                    "decimals": source_decimals,
                                    "symbol": asset,
                                },
                                dest_token={
                                    "address": dest_contract,
                                    "contract": dest_contract,
                                    "decimals": dest_decimals,
                                    "symbol": asset,
                                },
                                amount_atomic=bridge_amount_atomic,
                            )
                    route = build_live_bridge_route(bridge_quote) if bridge_quote else None
                    if route:
                        direct_live_routes.append(route)
                if direct_live_routes:
                    return max(
                        direct_live_routes,
                        key=lambda item: (
                            float(item.get("net_profit_usd", 0.0) or 0.0),
                            float(item.get("roi_pct", 0.0) or 0.0),
                        ),
                    )

                sell_proceeds_usd = tokens_after_buy * sell_price * (1.0 - dex_fee_rate)
                if sell_proceeds_usd > 0:
                    inventory_live_routes = []
                    for provider_id in live_bridge_provider_ids:
                        rebalance_quote = None
                        if provider_id == "relay":
                            rebalance_quote = await fetch_relay_rebalance_quote(
                                session,
                                source_chain=sell_quote["chain"],
                                dest_chain=buy_quote["chain"],
                                notional_usd=sell_proceeds_usd,
                            )
                        elif provider_id == "mayan":
                            rebalance_quote = await fetch_mayan_rebalance_quote(
                                session,
                                source_chain=sell_quote["chain"],
                                dest_chain=buy_quote["chain"],
                                notional_usd=sell_proceeds_usd,
                            )
                        elif provider_id == "wormhole":
                            rebalance_quote = await fetch_wormhole_rebalance_quote(
                                session,
                                source_chain=sell_quote["chain"],
                                dest_chain=buy_quote["chain"],
                                notional_usd=sell_proceeds_usd,
                            )
                        elif provider_id == "layerzero":
                            rebalance_quote = await fetch_layerzero_rebalance_quote(
                                session,
                                source_chain=sell_quote["chain"],
                                dest_chain=buy_quote["chain"],
                                notional_usd=sell_proceeds_usd,
                            )
                        elif provider_id == "debridge":
                            rebalance_quote = await fetch_debridge_rebalance_quote(
                                session,
                                source_chain=sell_quote["chain"],
                                dest_chain=buy_quote["chain"],
                                notional_usd=sell_proceeds_usd,
                            )
                        route = build_inventory_route(rebalance_quote) if rebalance_quote else None
                        if route:
                            inventory_live_routes.append(route)
                    if inventory_live_routes:
                        return max(
                            inventory_live_routes,
                            key=lambda item: (
                                float(item.get("net_profit_usd", 0.0) or 0.0),
                                float(item.get("roi_pct", 0.0) or 0.0),
                            ),
                        )

            bridge_cost_usd = gas_buffer_usd(buy_quote["chain"], bridge=True) + gas_buffer_usd(sell_quote["chain"])
            proceeds_usd = tokens_after_buy * sell_price * (1.0 - dex_fee_rate)
            net_profit_usd = proceeds_usd - notional_usd - bridge_cost_usd
            roi_pct = (net_profit_usd / notional_usd) * 100.0
            if spread < min_spread or net_profit_usd <= 0:
                return None

            return {
                "symbol": f"{asset}USDT",
                "asset": asset,
                "route_kind": "dex_bridge_dex",
                "buy_type": "dex",
                "sell_type": "dex",
                "buy_exchange": buy_quote["label"],
                "sell_exchange": sell_quote["label"],
                "buy_price": buy_price,
                "sell_price": sell_price,
                "spread": spread,
                "net_profit_usd": net_profit_usd,
                "roi_pct": roi_pct,
                "notional_usd": notional_usd,
                "buy_chain": buy_quote["chain"],
                "sell_chain": sell_quote["chain"],
                "chain": chains_label,
                "contract": buy_quote["contract"],
                "buy_contract": buy_quote["contract"],
                "sell_contract": sell_quote["contract"],
                "liquidity_usd": min(buy_quote["liquidity_usd"], sell_quote["liquidity_usd"]),
                "volume_24h": min(buy_quote["volume_24h"], sell_quote["volume_24h"]),
                "transfer_status": transfer_state_label(None, bridge_required=True),
                "withdraw_fee_asset": 0.0,
                "gas_estimate_usd": bridge_cost_usd,
                "bridge_required": True,
                "bridge_provider": top_bridge["name"],
                "bridge_docs_url": top_bridge.get("docs_url"),
                "bridge_candidates": bridge_candidates_payload,
                "execution_quality": "estimated",
                "quote_sources": [
                    str(buy_quote.get("quote_source") or "dex"),
                    str(sell_quote.get("quote_source") or "dex"),
                ],
                "buy_url": buy_quote.get("swap_url") or buy_quote["url"],
                "sell_url": sell_quote.get("swap_url") or sell_quote["url"],
                "notes": (
                    f"Estimated DEX-to-DEX route across chains. "
                    f"No live bridge quote was available; preferred bridge candidate: {top_bridge['name']}."
                ),
            }

        def is_major_candidate_asset(asset_name: str) -> bool:
            return str(asset_name or "").strip().upper() in major_quote_assets

        def build_candidate_assets() -> List[str]:
            if requested_assets:
                selected_assets: List[str] = []
                seen_selected = set()
                for requested_asset in requested_assets:
                    if requested_asset and requested_asset not in seen_selected:
                        seen_selected.add(requested_asset)
                        selected_assets.append(requested_asset)
                return selected_assets

            cached_opps = getattr(bot_instance, "cached_opportunities", None) or []
            try:
                ranked_cached = sorted(
                    cached_opps,
                    key=lambda opp: float(opp.get("spread", 0.0) or 0.0),
                    reverse=True,
                )
            except Exception:
                ranked_cached = list(cached_opps)

            cached_spread_by_asset: Dict[str, float] = {}
            asset_universe: List[str] = []
            seen_universe = set()

            for opp in ranked_cached:
                asset_name = extract_base_asset(opp.get("symbol"), assume_pair=True)
                if not asset_name:
                    continue
                spread_value = float(opp.get("spread", 0.0) or 0.0)
                current_best = float(cached_spread_by_asset.get(asset_name, 0.0) or 0.0)
                if spread_value > current_best:
                    cached_spread_by_asset[asset_name] = spread_value
                if asset_name not in seen_universe:
                    seen_universe.add(asset_name)
                    asset_universe.append(asset_name)

            symbol_frequency: Dict[str, int] = {}
            for exchange_name in enabled_exchanges:
                ex_map = snapshot.get(exchange_name) or {}
                if not isinstance(ex_map, dict):
                    continue
                for symbol in ex_map.keys():
                    base, quote = split_pair_symbol(symbol)
                    if not base or quote not in ("USDT", "USDC", "USD"):
                        continue
                    symbol_frequency[base] = symbol_frequency.get(base, 0) + 1
                    if base not in seen_universe:
                        seen_universe.add(base)
                        asset_universe.append(base)

            def asset_sort_key(asset_name: str) -> Tuple[Any, ...]:
                asset_u = str(asset_name or "").strip().upper()
                exchange_count = int(symbol_frequency.get(asset_u, 0) or 0)
                best_spread = float(cached_spread_by_asset.get(asset_u, 0.0) or 0.0)
                from_cached = 1 if asset_u in cached_spread_by_asset else 0
                is_major = is_major_candidate_asset(asset_u)
                rarity_bucket = max(0, 6 - max(exchange_count, 1))

                if asset_profile == "majors":
                    return (
                        1 if is_major else 0,
                        best_spread,
                        exchange_count,
                        from_cached,
                        asset_u,
                    )
                if asset_profile == "long_tail":
                    return (
                        0 if is_major else 1,
                        1 if 1 <= exchange_count <= 3 else 0,
                        rarity_bucket,
                        best_spread,
                        from_cached,
                        -exchange_count,
                        asset_u,
                    )
                return (
                    best_spread,
                    0 if is_major else 1,
                    1 if 1 <= exchange_count <= 4 else 0,
                    rarity_bucket,
                    from_cached,
                    -exchange_count,
                    asset_u,
                )

            ranked_assets = sorted(asset_universe, key=asset_sort_key, reverse=True)
            return ranked_assets[:asset_limit]

        candidate_assets: List[str] = build_candidate_assets()

        results: List[Dict[str, Any]] = []
        dex_quotes_found = 0
        jupiter_solana_hits = 0
        live_bridge_routes = 0
        inventory_routes = 0
        assets_scanned = 0
        timed_out_assets = 0
        failed_assets = 0
        scan_started_monotonic = time.monotonic()
        scan_budget_sec = 16.0 if quick_mode else 30.0
        # `per_asset_timeout_sec` is defined earlier and may be overridden by
        # the request param `per_asset_timeout`. Do not reassign it here.
        try:
            configured_parallel_assets = int(
                request.args.get(
                    "parallel_assets",
                    bot_instance.config.get(
                        "interchain_parallel_assets_quick" if quick_mode else "interchain_parallel_assets",
                        3 if quick_mode else 4,
                    ),
                ) or 0
            )
        except Exception:
            configured_parallel_assets = 3 if quick_mode else 4
        parallel_assets = max(1, min(configured_parallel_assets, 8))
        scan_deadline = scan_started_monotonic + scan_budget_sec

        async def process_asset(asset: str) -> Dict[str, Any]:
            asset_routes: List[Dict[str, Any]] = []
            asset_dex_quotes_found = 0
            asset_jupiter_hit = 0
            asset_live_bridge_routes = 0
            asset_inventory_routes = 0

            rows = await get_asset_rows(asset)
            contracts = await merge_contracts(asset, rows)
            preferred_solana_mint = str(((contracts.get("solana") or {}).get("contract")) or "").strip() or None

            solana_token = await resolve_solana_token(
                session,
                asset,
                preferred_mint=preferred_solana_mint,
                allow_symbol_only=False,
            )
            if solana_token and solana_token.get("contract"):
                existing_solana_meta = contracts.get("solana") or {}
                contracts["solana"] = {
                    **existing_solana_meta,
                    "contract": solana_token["contract"],
                    "source": str(solana_token.get("source") or "jupiter").strip().lower() or "jupiter",
                    "decimals": solana_token.get("decimals") or existing_solana_meta.get("decimals"),
                    "verified": bool(solana_token.get("verified")) or bool(existing_solana_meta.get("verified")),
                }
                if solana_token.get("source") == "jupiter_search":
                    asset_jupiter_hit += 1
            contracts = trim_contracts(contracts, asset)

            if not contracts:
                return {
                    "routes": asset_routes,
                    "dex_quotes_found": asset_dex_quotes_found,
                    "jupiter_hit": asset_jupiter_hit,
                    "live_bridge_routes": asset_live_bridge_routes,
                    "inventory_routes": asset_inventory_routes,
                }

            dex_quotes = await fetch_dex_quotes(session, asset, contracts, solana_token=solana_token)
            if not dex_quotes:
                return {
                    "routes": asset_routes,
                    "dex_quotes_found": asset_dex_quotes_found,
                    "jupiter_hit": asset_jupiter_hit,
                    "live_bridge_routes": asset_live_bridge_routes,
                    "inventory_routes": asset_inventory_routes,
                }
            asset_dex_quotes_found += len(dex_quotes)

            cex_quotes = pick_cex_quotes(asset)
            for cex_quote in cex_quotes:
                for dex_quote in dex_quotes:
                    transfer_row = find_transfer_row(rows, cex_quote["exchange"], dex_quote["chain"])
                    route_1 = build_cex_to_dex(asset, cex_quote, dex_quote, transfer_row)
                    if route_1:
                        asset_routes.append(route_1)
                    route_2 = build_dex_to_cex(asset, dex_quote, cex_quote, transfer_row)
                    if route_2:
                        asset_routes.append(route_2)

            if len(dex_quotes) > 1:
                cross_chain_quotes = sorted(
                    dex_quotes,
                    key=lambda item: (1 if str(item.get("quote_mode") or "") == "live" else 0, float(item.get("score", 0.0) or 0.0)),
                    reverse=True,
                )[:max_cross_chain_quotes]
                for buy_quote in cross_chain_quotes:
                    for sell_quote in cross_chain_quotes:
                        route_3 = await build_dex_bridge_dex(session, asset, buy_quote, sell_quote)
                        if route_3:
                            if route_3.get("route_kind") == "dex_bridge_dex_live":
                                asset_live_bridge_routes += 1
                            elif route_3.get("route_kind") == "dex_inventory_rebalance":
                                asset_inventory_routes += 1
                            asset_routes.append(route_3)

            return {
                "routes": asset_routes,
                "dex_quotes_found": asset_dex_quotes_found,
                "jupiter_hit": asset_jupiter_hit,
                "live_bridge_routes": asset_live_bridge_routes,
                "inventory_routes": asset_inventory_routes,
            }

        async with aiohttp.ClientSession(headers={"User-Agent": "arbx/1.0 (+interchain-scanner)"}) as session:
            asset_scan_semaphore = asyncio.Semaphore(parallel_assets)

            async def process_asset_with_limits(asset: str) -> Dict[str, Any]:
                async with asset_scan_semaphore:
                    remaining_budget = scan_deadline - time.monotonic()
                    if remaining_budget <= 0:
                        return {"status": "budget_exhausted", "asset": asset}

                    timeout_for_asset = max(0.75, min(per_asset_timeout_sec, remaining_budget))
                    try:
                        asset_result = await asyncio.wait_for(process_asset(asset), timeout=timeout_for_asset)
                        asset_result["status"] = "ok"
                        asset_result["asset"] = asset
                        return asset_result
                    except TimeoutError:
                        return {"status": "timeout", "asset": asset, "timeout_sec": timeout_for_asset}
                    except Exception as exc:
                        logging.debug("Interchain asset scan failed for %s: %s", asset, exc)
                        return {"status": "error", "asset": asset}

            asset_tasks = [asyncio.create_task(process_asset_with_limits(asset)) for asset in candidate_assets]
            budget_reached = False
            result_cap_reached = False
            try:
                for completed_task in asyncio.as_completed(asset_tasks):
                    elapsed = time.monotonic() - scan_started_monotonic
                    if elapsed >= scan_budget_sec:
                        budget_reached = True
                        break

                    asset_result = await completed_task
                    status = str(asset_result.get("status") or "").strip().lower()

                    if status == "budget_exhausted":
                        budget_reached = True
                        continue

                    assets_scanned += 1
                    if status == "timeout":
                        timed_out_assets += 1
                        logging.info(
                            "Interchain asset scan timed out for %s after %.1fs; skipping asset and keeping partial results",
                            asset_result.get("asset"),
                            float(asset_result.get("timeout_sec") or per_asset_timeout_sec),
                        )
                        continue
                    if status == "error":
                        failed_assets += 1
                        continue

                    results.extend(asset_result.get("routes") or [])
                    dex_quotes_found += int(asset_result.get("dex_quotes_found") or 0)
                    jupiter_solana_hits += int(asset_result.get("jupiter_hit") or 0)
                    live_bridge_routes += int(asset_result.get("live_bridge_routes") or 0)
                    inventory_routes += int(asset_result.get("inventory_routes") or 0)

                    if len(results) >= limit * 6:
                        result_cap_reached = True
                        break
            finally:
                for task in asset_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*asset_tasks, return_exceptions=True)

            if budget_reached:
                elapsed = time.monotonic() - scan_started_monotonic
                logging.info(
                    "Interchain scan reached internal budget after %.1fs; returning partial results (%s/%s assets, timed_out=%s, failed=%s, parallel=%s)",
                    elapsed,
                    assets_scanned,
                    len(candidate_assets),
                    timed_out_assets,
                    failed_assets,
                    parallel_assets,
                )
            elif result_cap_reached:
                logging.info(
                    "Interchain scan reached result cap after %s scanned assets; returning early (parallel=%s)",
                    assets_scanned,
                    parallel_assets,
                )

        routes_before_quality_filter = len(results)
        if execution_quality_filter:
            if execution_quality_filter == "actionable":
                allowed_qualities = {"live", "hybrid"}
            else:
                allowed_qualities = {execution_quality_filter}
            results = [
                item for item in results
                if str(item.get("execution_quality") or "").strip().lower() in allowed_qualities
            ]

        routes_before_group_filter = len(results)
        if route_group == "cex_dex":
            allowed_route_kinds = {"cex_to_dex", "dex_to_cex"}
            results = [item for item in results if str(item.get("route_kind") or "").strip().lower() in allowed_route_kinds]
        elif route_group == "cross_chain":
            allowed_route_kinds = {"dex_bridge_dex", "dex_bridge_dex_live", "dex_inventory_rebalance"}
            results = [item for item in results if str(item.get("route_kind") or "").strip().lower() in allowed_route_kinds]

        routes_before_scope_filter = len(results)
        if chain_scope != "all":
            results = [item for item in results if route_matches_scope(item)]

        results.sort(
            key=lambda item: (
                float(item.get("net_profit_usd", 0.0) or 0.0),
                float(item.get("roi_pct", 0.0) or 0.0),
                float(item.get("spread", 0.0) or 0.0),
            ),
            reverse=True,
        )

        return {
            "data": results[:limit],
            "statistics": {
                "assets_scanned": assets_scanned,
                "assets_considered": len(candidate_assets),
                "timed_out_assets": timed_out_assets,
                "failed_assets": failed_assets,
                "requested_assets": list(requested_assets),
                "routes_found": len(results),
                "dex_quotes_found": dex_quotes_found,
                "jupiter_solana_hits": jupiter_solana_hits,
                "live_bridge_routes": live_bridge_routes,
                "inventory_routes": inventory_routes,
                "routes_before_execution_quality_filter": routes_before_quality_filter,
                "routes_before_route_group_filter": routes_before_group_filter,
                "routes_before_chain_scope_filter": routes_before_scope_filter,
                "notional_usd": notional_usd,
                "bridge_provider_priority": list(preferred_bridge_providers),
                "live_bridge_provider_priority": list(live_bridge_provider_ids),
                "quick_mode": quick_mode,
                "parallel_assets": parallel_assets,
                "live_bridge_quotes": live_bridge_quotes,
                "route_group": route_group,
                "chain_scope": chain_scope,
                "asset_profile": asset_profile,
                "execution_quality_filter": execution_quality_filter or None,
            },
        }

    try:
        scan_result = asyncio.run(asyncio.wait_for(collect_interchain_data(), timeout=45))
    except TimeoutError:
        logging.warning("Interchain scan timed out after 45s")
        return jsonify({
            "success": False,
            "error": "Interchain scan timed out",
            "data": [],
            "statistics": {
                "notional_usd": notional_usd,
                "bridge_provider_priority": list(bot_instance.config.get("interchain_bridge_provider_priority", []) or []),
                "live_bridge_provider_priority": list(bot_instance.config.get("interchain_live_bridge_provider_priority", []) or ["mayan", "wormhole", "layerzero", "relay", "debridge"]),
                "route_group": route_group,
                "chain_scope": chain_scope,
                "asset_profile": asset_profile,
            },
            "snapshot_ts": snapshot_ts if snapshot_ts > 0 else None,
        }), 200

    snapshot_age_sec = int(max(0.0, time.time() - snapshot_ts)) if snapshot_ts > 0 else None
    try:
        stale_after = float(bot_instance.config.get("monitor_interval", 60) or 60) * 2.0
    except Exception:
        stale_after = 120.0

    payload = {
        "success": True,
        "data": scan_result.get("data") or [],
        "statistics": scan_result.get("statistics") or {},
        "snapshot_ts": snapshot_ts if snapshot_ts > 0 else None,
        "snapshot_age_sec": snapshot_age_sec,
        "snapshot_stale": bool(snapshot_age_sec is not None and snapshot_age_sec >= stale_after),
    }
    interchain_scan_cache[cache_key] = {"ts": time.time(), "payload": payload}
    interchain_scan_cache.move_to_end(cache_key)
    if len(interchain_scan_cache) > 12:
        interchain_scan_cache.popitem(last=False)
    return jsonify(payload)

@app.route('/api/interchain_debug')
def get_interchain_debug():
    """Debug one asset through the interchain discovery pipeline."""
    global bot_instance, asset_status_cache, dex_quote_cache
    if not bot_instance:
        return jsonify({"success": False, "error": "Bot not initialized"}), 200

    payload = handle_contract_first_interchain_debug(
        bot_instance,
        request.args,
        asset_status_cache,
        dex_quote_cache,
        asset_status_ttl=ASSET_STATUS_TTL,
        dex_quote_ttl=DEX_QUOTE_TTL,
    )
    return jsonify(payload), 200

    asset_raw = str(request.args.get('asset', '') or '').strip().upper()
    asset = extract_base_asset(asset_raw, assume_pair=True) or asset_raw
    if not asset:
        return jsonify({"success": False, "error": "Asset is required"}), 400

    live_bridge_quotes = str(request.args.get('live_bridge_quotes', '1')).strip().lower() not in ('0', 'false', 'no', 'off')
    chain_scope = str(request.args.get('chain_scope', 'all') or 'all').strip().lower()
    if chain_scope not in ('all', 'major', 'small'):
        chain_scope = 'all'
    try:
        notional_usd = float(request.args.get('notional_usd', bot_instance.config.get("arb_min_notional_usd", 100.0)) or 100.0)
    except Exception:
        notional_usd = 100.0
    notional_usd = max(10.0, min(notional_usd, 5000.0))

    snapshot = getattr(bot_instance, "_last_all_prices", None)
    snapshot_ts = float(getattr(bot_instance, "_last_all_prices_ts", 0.0) or 0.0)
    if not isinstance(snapshot, dict) or not snapshot:
        return jsonify({
            "success": False,
            "error": "No ticker snapshot available yet. Wait for the next monitor iteration.",
        }), 200

    enabled_exchange_objs = []
    enabled_exchanges = []
    try:
        calc_obj = getattr(bot_instance, "calc", None)
        if calc_obj and hasattr(calc_obj, "get_enabled_exchanges"):
            enabled_exchange_objs = list(calc_obj.get_enabled_exchanges())
            enabled_exchanges = [ex.name for ex in enabled_exchange_objs]
        elif calc_obj and hasattr(calc_obj, "exchanges"):
            enabled_exchange_objs = [ex for ex in calc_obj.exchanges if getattr(ex, "enabled", False)]
            enabled_exchanges = [ex.name for ex in enabled_exchange_objs]
    except Exception:
        enabled_exchange_objs = []
        enabled_exchanges = []
    if not enabled_exchanges:
        enabled_exchanges = [name for name, prices in snapshot.items() if isinstance(prices, dict)]

    async def collect_debug_data():
        def to_float(value: Any) -> Optional[float]:
            try:
                if value in (None, "", "null"):
                    return None
                return float(value)
            except Exception:
                return None

        def canon_chain(name: str) -> str:
            return canon_chain_name(name)

        def exchange_key(name: str) -> str:
            return "".join(ch for ch in str(name or "").lower() if ch.isalnum())

        def exchange_fee_rate(exchange_name: str) -> float:
            try:
                return max(0.0, float(bot_instance.config.get_exchange_fee(exchange_name, 0.1))) / 100.0
            except Exception:
                return 0.001

        def stable_price_band(asset_name: str) -> Optional[Tuple[float, float]]:
            if str(asset_name or "").strip().upper() in {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USD1"}:
                return (0.85, 1.15)
            return None

        def stable_prices_ok(asset_name: str, *prices: float) -> bool:
            band = stable_price_band(asset_name)
            if not band:
                return True
            low, high = band
            for price in prices:
                try:
                    value = float(price)
                except Exception:
                    continue
                if value <= 0:
                    continue
                if value < low or value > high:
                    return False
            return True

        def cross_chain_identity_ok(buy_quote: Dict[str, Any], sell_quote: Dict[str, Any]) -> bool:
            if buy_quote.get("chain") == sell_quote.get("chain"):
                return True
            asset_u = str(asset or "").strip().upper()
            if asset_u in {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USD1"}:
                if str(buy_quote.get("contract") or "").strip() and str(sell_quote.get("contract") or "").strip():
                    return True
            trusted_sources = {"coingecko", "jupiter", "mayan"}
            buy_identity = str(buy_quote.get("identity_key") or "").strip().lower()
            sell_identity = str(sell_quote.get("identity_key") or "").strip().lower()
            if buy_identity and sell_identity:
                return buy_identity == sell_identity
            buy_family = str(buy_quote.get("family_key") or "").strip().lower()
            sell_family = str(sell_quote.get("family_key") or "").strip().lower()
            buy_source = str(buy_quote.get("contract_source") or "").strip().lower()
            sell_source = str(sell_quote.get("contract_source") or "").strip().lower()
            if buy_family and sell_family and buy_family == sell_family:
                if buy_family.startswith(("identity:", "origin:", "cg:")):
                    return True
                if (buy_quote.get("verified") and sell_quote.get("verified")) or (
                    buy_source in trusted_sources and sell_source in trusted_sources
                ):
                    return True
            buy_origin_chain = str(buy_quote.get("origin_chain") or "").strip().lower()
            sell_origin_chain = str(sell_quote.get("origin_chain") or "").strip().lower()
            buy_origin_contract = str(buy_quote.get("origin_contract") or "").strip().lower()
            sell_origin_contract = str(sell_quote.get("origin_contract") or "").strip().lower()
            if buy_origin_chain and sell_origin_chain and buy_origin_contract and sell_origin_contract:
                return buy_origin_chain == sell_origin_chain and buy_origin_contract == sell_origin_contract
            buy_cg = str(buy_quote.get("coingecko_id") or "").strip().lower()
            sell_cg = str(sell_quote.get("coingecko_id") or "").strip().lower()
            if buy_cg and sell_cg and buy_cg == sell_cg and (buy_quote.get("verified") or sell_quote.get("verified")):
                return True
            if buy_source not in trusted_sources or sell_source not in trusted_sources:
                return False
            chains = {str(buy_quote.get("chain") or ""), str(sell_quote.get("chain") or "")}
            if "solana" in chains:
                return "jupiter" in {buy_source, sell_source}
            return buy_source == "coingecko" and sell_source == "coingecko"

        async def get_asset_rows(asset_name: str) -> List[Dict[str, Any]]:
            now = time.time()
            cached = asset_status_cache.get(asset_name)
            if cached and (now - cached.get("ts", 0) < ASSET_STATUS_TTL):
                data = cached.get("data") or []
                return list(data) if isinstance(data, list) else []
            try:
                data = await asyncio.wait_for(
                    _prefetch_asset_status_async(asset_name, enabled_exchange_objs),
                    timeout=3.5,
                )
                if data:
                    asset_status_cache[asset_name] = {"ts": time.time(), "data": data}
                return list(data or [])
            except Exception as exc:
                logging.debug("interchain debug asset status fetch failed for %s: %s", asset_name, exc)
                return []

        async def merge_contracts(asset_name: str, rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
            contracts: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                chain = canon_chain(row.get("chain"))
                contract = str(
                    row.get("contract_address")
                    or row.get("contract")
                    or ""
                ).strip()
                if not contract or contract.lower() in ("-", "native", "native coin", "native token"):
                    continue
                if chain not in contracts:
                    contracts[chain] = {
                        "contract": contract,
                        "source": "exchange_info",
                    }

            cached_cg = coingecko_platforms_cache.get(asset_name)
            cg_coin_id = None
            cg_family_key = None
            if isinstance(cached_cg, dict):
                cg_coin_id = str(cached_cg.get("coin_id") or "").strip() or None
                cg_family_key = f"cg:{cg_coin_id.lower()}" if cg_coin_id else None
            if cached_cg and (time.time() - cached_cg.get("ts", 0) < COINGECKO_PLATFORMS_TTL):
                platforms = cached_cg.get("platforms") or {}
                if isinstance(platforms, dict):
                    for chain_raw, contract in platforms.items():
                        contract_str = str(contract or "").strip()
                        if not contract_str:
                            continue
                        chain = canon_chain(chain_raw)
                        current = contracts.get(chain)
                        incoming = {
                            "contract": contract_str,
                            "source": "coingecko",
                            "coingecko_id": cg_coin_id,
                            "family_key": cg_family_key,
                        }
                        if not current:
                            contracts[chain] = incoming
                            continue
                        current_source = str(current.get("source") or "").strip().lower()
                        if contract_str and current_source not in {"mayan", "jupiter"}:
                            current["contract"] = contract_str
                            current["source"] = "coingecko"
                        if cg_coin_id and not current.get("coingecko_id"):
                            current["coingecko_id"] = cg_coin_id
                        if cg_family_key and not current.get("family_key"):
                            current["family_key"] = cg_family_key

            preferred_contracts = [
                str((meta or {}).get("contract") or "").strip()
                for meta in contracts.values()
                if str((meta or {}).get("contract") or "").strip()
            ]
            try:
                mayan_tokens = await resolve_mayan_asset_tokens(
                    session,
                    asset_name,
                    preferred_contracts=preferred_contracts,
                )
            except Exception as exc:
                logging.debug("interchain debug Mayan registry merge failed for %s: %s", asset_name, exc)
                mayan_tokens = []

            for token in mayan_tokens:
                chain = canon_chain(token.get("chain"))
                if not chain:
                    continue
                incoming = {
                    "contract": str(token.get("contract") or "").strip(),
                    "source": "mayan",
                    "decimals": int(token.get("decimals") or 0) if token.get("decimals") is not None else None,
                    "verified": bool(token.get("verified")),
                    "identity_key": str(token.get("identity_key") or "").strip() or None,
                    "family_key": str(token.get("family_key") or "").strip() or None,
                    "origin_chain": str(token.get("origin_chain") or "").strip() or None,
                    "origin_contract": str(token.get("origin_contract") or "").strip() or None,
                    "coingecko_id": str(token.get("coingecko_id") or "").strip() or None,
                }
                current = contracts.get(chain)
                if not current:
                    contracts[chain] = incoming
                    continue
                current_source = str(current.get("source") or "").strip().lower()
                if current_source == "coingecko" and incoming.get("contract"):
                    current["contract"] = incoming["contract"]
                    current["source"] = "mayan"
                if not current.get("decimals") and incoming.get("decimals"):
                    current["decimals"] = incoming["decimals"]
                if incoming.get("verified"):
                    current["verified"] = True
                for key in ("identity_key", "family_key", "origin_chain", "origin_contract", "coingecko_id"):
                    if not current.get(key) and incoming.get(key):
                        current[key] = incoming[key]

            need_symbol_discovery = chain_scope == "small" or len(contracts) <= 1
            if need_symbol_discovery:
                try:
                    discovered_tokens = await discover_symbol_contracts(
                        session,
                        asset_name,
                        preferred_contracts=preferred_contracts,
                    )
                except Exception as exc:
                    logging.debug("interchain debug symbol discovery failed for %s: %s", asset_name, exc)
                    discovered_tokens = []
                for token in discovered_tokens:
                    chain = canon_chain(token.get("chain"))
                    if not chain:
                        continue
                    incoming = {
                        "contract": str(token.get("contract") or "").strip(),
                        "source": str(token.get("source") or "unknown").strip().lower() or "unknown",
                        "decimals": int(token.get("decimals") or 0) if token.get("decimals") is not None else None,
                        "verified": bool(token.get("verified")),
                        "identity_key": str(token.get("identity_key") or "").strip() or None,
                        "family_key": str(token.get("family_key") or "").strip() or None,
                        "origin_chain": str(token.get("origin_chain") or "").strip() or None,
                        "origin_contract": str(token.get("origin_contract") or "").strip() or None,
                        "coingecko_id": str(token.get("coingecko_id") or "").strip() or None,
                    }
                    current = contracts.get(chain)
                    if not current:
                        contracts[chain] = incoming
                        continue
                    for key in ("decimals", "identity_key", "family_key", "origin_chain", "origin_contract", "coingecko_id"):
                        if not current.get(key) and incoming.get(key):
                            current[key] = incoming[key]
            return contracts

        def pick_cex_quotes(asset_name: str) -> List[Dict[str, Any]]:
            quote_priority = {"USDT": 0, "USDC": 1, "USD": 2}
            quotes: List[Dict[str, Any]] = []
            for exchange_name in enabled_exchanges:
                ex_map = snapshot.get(exchange_name) or {}
                if not isinstance(ex_map, dict):
                    continue
                best = None
                for symbol, raw_price in ex_map.items():
                    price = to_float(raw_price)
                    if price is None or price <= 0:
                        continue
                    base, quote = split_pair_symbol(symbol)
                    if base != asset_name or quote not in quote_priority:
                        continue
                    candidate = {
                        "exchange": exchange_name,
                        "symbol": symbol,
                        "price": float(price),
                        "quote": quote,
                        "priority": quote_priority[quote],
                        "fee_rate": exchange_fee_rate(exchange_name),
                    }
                    if best is None or candidate["priority"] < best["priority"]:
                        best = candidate
                if best:
                    quotes.append(best)
            quotes.sort(key=lambda item: (item["priority"], item["price"]))
            return quotes

        def parse_dex_pairs(asset_name: str, expected_chain: str, contract: str, payload: Any, *, contract_source: str) -> List[Dict[str, Any]]:
            best_by_chain: Dict[str, Dict[str, Any]] = {}
            contract_lc = str(contract or "").strip().lower()
            pairs = payload.get("pairs") if isinstance(payload, dict) else []
            for pair in pairs or []:
                if not isinstance(pair, dict):
                    continue
                base_token = pair.get("baseToken") or {}
                if str(base_token.get("address") or "").strip().lower() != contract_lc:
                    continue
                price = to_float(pair.get("priceUsd"))
                if price is None or price <= 0:
                    continue
                chain_id = canon_chain(pair.get("chainId"))
                if expected_chain and chain_id != expected_chain:
                    continue
                liquidity_usd = to_float((pair.get("liquidity") or {}).get("usd")) or 0.0
                volume_24h = to_float((pair.get("volume") or {}).get("h24")) or 0.0
                score = (liquidity_usd * 10.0) + volume_24h
                item = {
                    "asset": asset_name,
                    "chain": chain_id,
                    "contract": contract,
                    "contract_source": contract_source,
                    "price": float(price),
                    "liquidity_usd": float(liquidity_usd),
                    "volume_24h": float(volume_24h),
                    "dex_id": str(pair.get("dexId") or "dex").strip() or "dex",
                    "pair_address": str(pair.get("pairAddress") or "").strip(),
                    "url": f"https://dexscreener.com/{pair.get('chainId')}/{pair.get('pairAddress')}",
                    "label": f"{str(pair.get('dexId') or 'DEX').strip()} ({chain_id})",
                    "score": float(score),
                    "quote_source": "dexscreener",
                    "quote_mode": "estimated",
                    "route_labels": [],
                }
                current = best_by_chain.get(chain_id)
                if current is None or item["score"] > current["score"]:
                    best_by_chain[chain_id] = item
            return list(best_by_chain.values())

        async def fetch_dex_quotes(
            session: aiohttp.ClientSession,
            asset_name: str,
            contracts: Dict[str, Dict[str, Any]],
            solana_token: Optional[Dict[str, Any]] = None,
        ) -> List[Dict[str, Any]]:
            quotes: List[Dict[str, Any]] = []

            if solana_token and solana_token.get("contract"):
                jupiter_quote = await build_jupiter_dex_quote(
                    session,
                    symbol=asset_name,
                    mint=solana_token.get("contract"),
                    decimals=solana_token.get("decimals"),
                    notional_usd=notional_usd,
                    usd_hint=solana_token.get("price"),
                    metadata=solana_token,
                )
                if jupiter_quote:
                    solana_meta = contracts.get("solana") or {}
                    for field in ("identity_key", "family_key", "origin_chain", "origin_contract", "coingecko_id", "decimals"):
                        if solana_meta.get(field) not in (None, "", [], {}):
                            jupiter_quote[field] = solana_meta.get(field)
                    if solana_meta.get("verified"):
                        jupiter_quote["verified"] = True
                    quotes.append(jupiter_quote)

            for chain, contract_meta in contracts.items():
                contract_str = str((contract_meta or {}).get("contract") or "").strip()
                contract_source = str((contract_meta or {}).get("source") or "unknown").strip().lower() or "unknown"
                if not contract_str:
                    continue
                contract_meta_overlay = {}
                for field in ("identity_key", "family_key", "origin_chain", "origin_contract", "coingecko_id", "decimals"):
                    if (contract_meta or {}).get(field) not in (None, "", [], {}):
                        contract_meta_overlay[field] = (contract_meta or {}).get(field)
                if (contract_meta or {}).get("verified"):
                    contract_meta_overlay["verified"] = True
                quote_cache_key = f"{chain}:{contract_str.lower()}"
                cached_quote = dex_quote_cache.get(quote_cache_key)
                payload = None
                if cached_quote and (time.time() - cached_quote.get("ts", 0) < DEX_QUOTE_TTL):
                    payload = cached_quote.get("data")
                if payload is None:
                    url = f"https://api.dexscreener.com/latest/dex/tokens/{contract_str}"
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                            if response.status == 200:
                                payload = await response.json(content_type=None)
                                dex_quote_cache[quote_cache_key] = {"ts": time.time(), "data": payload}
                    except Exception as exc:
                        logging.debug("interchain debug DexScreener fetch failed for %s %s: %s", asset_name, contract_str, exc)
                dexscreener_quotes = parse_dex_pairs(asset_name, chain, contract_str, payload, contract_source=contract_source) if payload is not None else []
                if dexscreener_quotes and contract_meta_overlay:
                    for item in dexscreener_quotes:
                        item.update(contract_meta_overlay)
                if dexscreener_quotes:
                    quotes.extend(dexscreener_quotes)
                else:
                    try:
                        gecko_quotes = await fetch_geckoterminal_dex_quotes(
                            session,
                            asset=asset_name,
                            chain=chain,
                            contract=contract_str,
                            contract_source=contract_source,
                        )
                        if gecko_quotes and contract_meta_overlay:
                            for item in gecko_quotes:
                                item.update(contract_meta_overlay)
                        quotes.extend(gecko_quotes)
                    except Exception as exc:
                        logging.debug("interchain debug GeckoTerminal fetch failed for %s %s %s: %s", asset_name, chain, contract_str, exc)

            best_quotes: Dict[str, Dict[str, Any]] = {}
            for quote in quotes:
                current = best_quotes.get(quote["chain"])
                quote_rank = 1 if str(quote.get("quote_mode") or "") == "live" else 0
                current_rank = 1 if current and str(current.get("quote_mode") or "") == "live" else 0
                if current is None or (quote_rank, quote["score"]) > (current_rank, current["score"]):
                    best_quotes[quote["chain"]] = quote
            return list(best_quotes.values())

        def summarize_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
            by_exchange: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                exchange_name = str(row.get("exchange") or "").strip() or "unknown"
                item = by_exchange.setdefault(exchange_name, {
                    "rows": 0,
                    "chains": [],
                    "deposit_true": 0,
                    "withdraw_true": 0,
                    "known_rows": 0,
                })
                item["rows"] += 1
                chain_name = str(row.get("chain") or "").strip() or "-"
                if chain_name not in item["chains"]:
                    item["chains"].append(chain_name)
                if row.get("deposit_enabled") is not None or row.get("withdraw_enabled") is not None:
                    item["known_rows"] += 1
                if row.get("deposit_enabled") is True:
                    item["deposit_true"] += 1
                if row.get("withdraw_enabled") is True:
                    item["withdraw_true"] += 1
            return {
                "total_rows": len(rows),
                "exchange_count": len(by_exchange),
                "by_exchange": by_exchange,
            }

        preferred_live_bridge_providers = bot_instance.config.get("interchain_live_bridge_provider_priority", []) or ["mayan", "wormhole", "layerzero", "relay", "debridge"]
        live_bridge_blacklist = bot_instance.config.get("interchain_live_bridge_provider_blacklist", []) or []
        live_bridge_blacklist_ids = {str(item).strip().lower() for item in live_bridge_blacklist if str(item or "").strip()}
        live_bridge_provider_ids = []
        if live_bridge_quotes:
            for provider in preferred_live_bridge_providers:
                provider_id = str(provider or "").strip().lower()
                if provider_id and provider_id not in live_bridge_blacklist_ids and provider_id not in live_bridge_provider_ids:
                    live_bridge_provider_ids.append(provider_id)
            if not live_bridge_provider_ids:
                for provider_id in ("mayan", "wormhole", "layerzero", "relay", "debridge"):
                    if provider_id not in live_bridge_blacklist_ids:
                        live_bridge_provider_ids.append(provider_id)

        rows = await get_asset_rows(asset)

        async with aiohttp.ClientSession(headers={"User-Agent": "arbx/1.0 (+interchain-debug)"}) as session:
            contracts = await merge_contracts(asset, rows)
            preferred_solana_mint = str(((contracts.get("solana") or {}).get("contract")) or "").strip() or None
            solana_token = await resolve_solana_token(
                session,
                asset,
                preferred_mint=preferred_solana_mint,
                allow_symbol_only=False,
            )
            if solana_token and solana_token.get("contract"):
                existing_solana_meta = contracts.get("solana") or {}
                contracts["solana"] = {
                    **existing_solana_meta,
                    "contract": solana_token["contract"],
                    "source": str(solana_token.get("source") or "jupiter").strip().lower() or "jupiter",
                    "decimals": solana_token.get("decimals") or existing_solana_meta.get("decimals"),
                    "verified": bool(solana_token.get("verified")) or bool(existing_solana_meta.get("verified")),
                }

            dex_quotes = await fetch_dex_quotes(session, asset, contracts, solana_token=solana_token)
            cex_quotes = pick_cex_quotes(asset)
            relay_chains = await fetch_relay_chains(session) if "relay" in live_bridge_provider_ids else []
            cross_chain_attempts: List[Dict[str, Any]] = []
            is_stable_asset = str(asset or "").strip().upper() in {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USD1"}
            max_debug_cross_chain_quotes = 4 if is_stable_asset else 6
            provider_timeout_sec = 4.0
            ranked_dex_quotes = sorted(
                dex_quotes,
                key=lambda item: (1 if str(item.get("quote_mode") or "") == "live" else 0, float(item.get("score", 0.0) or 0.0)),
                reverse=True,
            )[:max_debug_cross_chain_quotes]

            for buy_quote in ranked_dex_quotes:
                for sell_quote in ranked_dex_quotes:
                    if buy_quote.get("chain") == sell_quote.get("chain"):
                        continue
                    buy_price = float(buy_quote.get("price") or 0.0)
                    sell_price = float(sell_quote.get("price") or 0.0)
                    gross_spread_pct = ((sell_price / buy_price) - 1.0) * 100.0 if buy_price > 0 and sell_price > 0 else None
                    attempt: Dict[str, Any] = {
                        "buy_chain": buy_quote.get("chain"),
                        "sell_chain": sell_quote.get("chain"),
                        "buy_label": buy_quote.get("label"),
                        "sell_label": sell_quote.get("label"),
                        "buy_contract": buy_quote.get("contract"),
                        "sell_contract": sell_quote.get("contract"),
                        "buy_contract_source": buy_quote.get("contract_source"),
                        "sell_contract_source": sell_quote.get("contract_source"),
                        "buy_price": buy_price,
                        "sell_price": sell_price,
                        "gross_spread_pct": gross_spread_pct,
                        "status": "pending",
                        "reason": "",
                        "bridge_candidates": [],
                        "providers": [],
                    }

                    if not stable_prices_ok(asset, buy_price, sell_price):
                        attempt["status"] = "rejected"
                        attempt["reason"] = "stable_price_out_of_band"
                        cross_chain_attempts.append(attempt)
                        continue

                    if not cross_chain_identity_ok(buy_quote, sell_quote):
                        attempt["status"] = "rejected"
                        attempt["reason"] = "identity_mismatch"
                        cross_chain_attempts.append(attempt)
                        continue

                    bridge_candidates = get_bridge_candidates(
                        asset,
                        buy_quote.get("chain"),
                        sell_quote.get("chain"),
                        preferred=bot_instance.config.get("interchain_bridge_provider_priority", []) or [],
                        blacklist=bot_instance.config.get("interchain_bridge_provider_blacklist", []) or [],
                    ) or []
                    attempt["bridge_candidates"] = [
                        {
                            "id": str(item.get("id") or "").strip(),
                            "name": str(item.get("name") or "").strip(),
                            "docs_url": item.get("docs_url"),
                        }
                        for item in bridge_candidates[:6]
                    ]
                    if not bridge_candidates:
                        attempt["status"] = "rejected"
                        attempt["reason"] = "no_bridge_candidates"
                        cross_chain_attempts.append(attempt)
                        continue

                    if not live_bridge_provider_ids:
                        attempt["status"] = "candidate_only"
                        attempt["reason"] = "live_bridge_quotes_disabled"
                        cross_chain_attempts.append(attempt)
                        continue

                    bridge_amount_tokens = (float(notional_usd) / buy_price) if buy_price > 0 else 0.0
                    if bridge_amount_tokens <= 0:
                        attempt["status"] = "rejected"
                        attempt["reason"] = "non_positive_bridge_amount"
                        cross_chain_attempts.append(attempt)
                        continue

                    provider_hits = 0
                    for provider_id in live_bridge_provider_ids:
                        provider_item: Dict[str, Any] = {"provider_id": provider_id, "status": "pending"}
                        source_token = None
                        dest_token = None
                        if provider_id == "relay":
                            source_token = get_relay_featured_token(
                                relay_chains,
                                buy_quote["chain"],
                                asset,
                                contract=buy_quote.get("contract"),
                            ) if relay_chains else None
                            dest_token = get_relay_featured_token(
                                relay_chains,
                                sell_quote["chain"],
                                asset,
                                contract=sell_quote.get("contract"),
                            ) if relay_chains else None
                        elif provider_id == "mayan":
                            source_token = await get_mayan_supported_token(
                                session,
                                buy_quote["chain"],
                                asset,
                                contract=buy_quote.get("contract"),
                            )
                            dest_token = await get_mayan_supported_token(
                                session,
                                sell_quote["chain"],
                                asset,
                                contract=sell_quote.get("contract"),
                            )
                        elif provider_id == "wormhole":
                            source_contract = str(buy_quote.get("contract") or "").strip()
                            dest_contract = str(sell_quote.get("contract") or "").strip()
                            if source_contract and dest_contract:
                                source_token = {
                                    "address": source_contract,
                                    "contract": source_contract,
                                    "decimals": int(buy_quote.get("decimals") or 6),
                                    "symbol": asset,
                                }
                                dest_token = {
                                    "address": dest_contract,
                                    "contract": dest_contract,
                                    "decimals": int(sell_quote.get("decimals") or 6),
                                    "symbol": asset,
                                }
                        elif provider_id == "debridge":
                            source_contract = str(buy_quote.get("contract") or "").strip()
                            dest_contract = str(sell_quote.get("contract") or "").strip()
                            if source_contract and dest_contract:
                                source_token = {
                                    "address": source_contract,
                                    "contract": source_contract,
                                    "decimals": int(buy_quote.get("decimals") or 0),
                                    "symbol": asset,
                                }
                        elif provider_id == "layerzero":
                            source_contract = str(buy_quote.get("contract") or "").strip()
                            dest_contract = str(sell_quote.get("contract") or "").strip()
                            if source_contract and dest_contract:
                                source_token = {
                                    "address": source_contract,
                                    "contract": source_contract,
                                    "decimals": int(buy_quote.get("decimals") or 0),
                                    "symbol": asset,
                                }
                                dest_token = {
                                    "address": dest_contract,
                                    "contract": dest_contract,
                                    "decimals": int(sell_quote.get("decimals") or 0),
                                    "symbol": asset,
                                }

                        if not source_token or not dest_token:
                            provider_item["status"] = "unsupported_token"
                            provider_item["reason"] = "provider_token_not_found"
                            attempt["providers"].append(provider_item)
                            continue

                        decimals = int(source_token.get("decimals") or 0)
                        if decimals <= 0:
                            provider_item["status"] = "unsupported_token"
                            provider_item["reason"] = "missing_decimals"
                            attempt["providers"].append(provider_item)
                            continue

                        amount_atomic = max(1, int(round(bridge_amount_tokens * (10 ** decimals))))
                        quote_payload = None
                        if provider_id == "relay":
                            quote_payload = await asyncio.wait_for(
                                fetch_relay_quote(
                                    session,
                                    source_chain=buy_quote["chain"],
                                    dest_chain=sell_quote["chain"],
                                    source_token=source_token,
                                    dest_token=dest_token,
                                    amount_atomic=amount_atomic,
                                ),
                                timeout=provider_timeout_sec,
                            )
                        elif provider_id == "mayan":
                            quote_payload = await asyncio.wait_for(
                                fetch_mayan_quote(
                                    session,
                                    source_chain=buy_quote["chain"],
                                    dest_chain=sell_quote["chain"],
                                    source_token=source_token,
                                    dest_token=dest_token,
                                    amount_atomic=amount_atomic,
                                ),
                                timeout=provider_timeout_sec,
                            )
                        elif provider_id == "wormhole":
                            quote_payload = await asyncio.wait_for(
                                fetch_wormhole_quote(
                                    session,
                                    source_chain=buy_quote["chain"],
                                    dest_chain=sell_quote["chain"],
                                    source_token=source_token,
                                    dest_token=dest_token,
                                    amount_atomic=amount_atomic,
                                ),
                                timeout=provider_timeout_sec,
                            )
                        elif provider_id == "layerzero":
                            quote_payload = await asyncio.wait_for(
                                fetch_layerzero_quote(
                                    session,
                                    source_chain=buy_quote["chain"],
                                    dest_chain=sell_quote["chain"],
                                    source_token=source_token,
                                    dest_token=dest_token,
                                    amount_atomic=amount_atomic,
                                ),
                                timeout=provider_timeout_sec,
                            )
                        elif provider_id == "debridge":
                            quote_payload = await asyncio.wait_for(
                                fetch_debridge_quote(
                                    session,
                                    source_chain=buy_quote["chain"],
                                    dest_chain=sell_quote["chain"],
                                    source_token=source_token,
                                    dest_token=dest_token,
                                    amount_atomic=amount_atomic,
                                ),
                                timeout=provider_timeout_sec,
                            )
                        if not quote_payload:
                            provider_item["status"] = "no_quote"
                            provider_item["reason"] = "provider_returned_empty_quote"
                            attempt["providers"].append(provider_item)
                            continue

                        provider_hits += 1
                        provider_item["status"] = "ok"
                        provider_item["amount_in_usd"] = quote_payload.get("amount_in_usd")
                        provider_item["amount_out_usd"] = quote_payload.get("amount_out_usd")
                        provider_item["wallet_gas_usd"] = quote_payload.get("wallet_gas_usd")
                        provider_item["relayer_fee_usd"] = quote_payload.get("relayer_fee_usd")
                        provider_item["time_estimate_sec"] = quote_payload.get("time_estimate_sec")
                        provider_item["rate"] = quote_payload.get("rate")
                        provider_item["router"] = quote_payload.get("router")
                        attempt["providers"].append(provider_item)

                    if provider_hits:
                        attempt["status"] = "bridge_quote_available"
                        attempt["reason"] = "ok"
                    else:
                        attempt["status"] = "bridge_quote_missing"
                        attempt["reason"] = "all_live_providers_failed"
                    cross_chain_attempts.append(attempt)

        return {
            "asset": asset,
            "notional_usd": notional_usd,
            "enabled_exchanges": enabled_exchanges,
            "asset_status_rows": rows,
            "asset_status_summary": summarize_rows(rows),
            "contracts": contracts,
            "solana_token": solana_token,
            "cex_quotes": cex_quotes,
            "dex_quotes": ranked_dex_quotes,
            "cross_chain_attempts": cross_chain_attempts,
            "live_bridge_provider_priority": list(live_bridge_provider_ids),
        }

    try:
        debug_data = asyncio.run(asyncio.wait_for(collect_debug_data(), timeout=45))
    except TimeoutError:
        return jsonify({"success": False, "error": "Interchain debug timed out"}), 200
    except Exception as exc:
        logging.error("Error in /api/interchain_debug for %s: %s\n%s", asset, exc, traceback.format_exc())
        return jsonify({"success": False, "error": str(exc)}), 200

    preview_groups: Dict[str, Any] = {}
    for route_group in ("cex_dex", "cross_chain"):
        try:
            with app.test_request_context(
                f"/api/interchain_opportunities?min_spread=0&limit=10&asset_limit=5&notional_usd={float(notional_usd)}"
                f"&assets={asset}&quick_mode=1&live_bridge_quotes={1 if live_bridge_quotes else 0}&route_group={route_group}"
            ):
                preview_response = app.make_response(get_interchain_opportunities())
                preview_payload = preview_response.get_json(silent=True) or {}
                preview_groups[route_group] = {
                    "success": bool(preview_payload.get("success")),
                    "error": preview_payload.get("error"),
                    "statistics": preview_payload.get("statistics") or {},
                    "routes": (preview_payload.get("data") or [])[:10],
                }
        except Exception as exc:
            preview_groups[route_group] = {
                "success": False,
                "error": str(exc),
                "statistics": {},
                "routes": [],
            }

    snapshot_age_sec = int(max(0.0, time.time() - snapshot_ts)) if snapshot_ts > 0 else None
    try:
        stale_after = float(bot_instance.config.get("monitor_interval", 60) or 60) * 2.0
    except Exception:
        stale_after = 120.0

    return jsonify({
        "success": True,
        "data": debug_data,
        "scanner_preview": preview_groups,
        "snapshot_ts": snapshot_ts if snapshot_ts > 0 else None,
        "snapshot_age_sec": snapshot_age_sec,
        "snapshot_stale": bool(snapshot_age_sec is not None and snapshot_age_sec >= stale_after),
    })

@app.route('/api/coin_arbitrage/<path:symbol>')
def get_coin_arbitrage_opportunities(symbol):
    """API for coin-specific arbitrage across exchanges (uses latest cached tickers).

    Important: this endpoint does NOT trigger live network calls to exchanges.
    It uses the most recent ticker snapshot collected by the monitoring loop.
    """
    global bot_instance
    if not bot_instance:
        logging.error("Бот не инициализирован")
        return jsonify({"success": False, "error": "Бот не инициализирован"})

    try:
        sym = (symbol or "").strip().upper()
        if not sym:
            return jsonify({"success": False, "error": "Символ не указан"}), 400

        # Enabled exchanges list (prefer calculator runtime state)
        enabled = []
        try:
            calc_obj = getattr(bot_instance, "calc", None)
            if calc_obj and hasattr(calc_obj, "get_enabled_exchanges"):
                enabled = [ex.name for ex in calc_obj.get_enabled_exchanges()]
            elif calc_obj and hasattr(calc_obj, "exchanges"):
                enabled = [ex.name for ex in calc_obj.exchanges if getattr(ex, "enabled", False)]
        except Exception:
            enabled = []
        if not enabled:
            try:
                enabled = list(bot_instance.config.get("enabled_exchanges", []) or [])
            except Exception:
                enabled = []

        snapshot = getattr(bot_instance, "_last_all_prices", None)
        snap_ts = float(getattr(bot_instance, "_last_all_prices_ts", 0.0) or 0.0)
        if not isinstance(snapshot, dict) or not snapshot:
            return jsonify({
                "success": False,
                "error": "Нет снимка тикеров. Подождите следующей итерации мониторинга.",
            }), 200

        prices: dict[str, float] = {}
        for ex_name in enabled:
            try:
                ex_map = snapshot.get(ex_name) or {}
                if not isinstance(ex_map, dict):
                    continue
                p = ex_map.get(sym)
                if p is None:
                    # Basic normalization fallback
                    p = ex_map.get(sym.replace("/", ""))
                if isinstance(p, (int, float)) and p > 0:
                    prices[str(ex_name)] = float(p)
            except Exception:
                continue

        ex_list = sorted(prices.keys())
        direct = []
        for buy_ex in ex_list:
            buy_price = prices.get(buy_ex)
            if not isinstance(buy_price, (int, float)) or buy_price <= 0:
                continue
            for sell_ex in ex_list:
                if sell_ex == buy_ex:
                    continue
                sell_price = prices.get(sell_ex)
                if not isinstance(sell_price, (int, float)) or sell_price <= 0:
                    continue
                spread = ((float(sell_price) / float(buy_price)) - 1.0) * 100.0
                direct.append({
                    "symbol": sym,
                    "buy_exchange": buy_ex,
                    "sell_exchange": sell_ex,
                    "buy_price": float(buy_price),
                    "sell_price": float(sell_price),
                    "spread": float(spread),
                    "timestamp": datetime.utcnow().isoformat(),
                })

        direct_pos = [o for o in direct if isinstance(o.get("spread"), (int, float)) and o["spread"] > 0]
        direct_pos.sort(key=lambda o: float(o.get("spread", 0.0)), reverse=True)

        if bool(bot_instance.config.get("ui_arb_filter_liquidity", False)):
            try:
                min_spread_req = float(bot_instance.config.get("min_spread", 0.0) or 0.0)
            except Exception:
                min_spread_req = 0.0
            cached = getattr(bot_instance, "cached_opportunities", []) or []
            filtered_direct = []
            if isinstance(cached, list):
                for opp in cached:
                    if not isinstance(opp, dict):
                        continue
                    if str(opp.get("symbol") or "").strip().upper() != sym:
                        continue
                    depth_spread = opp.get("depth_gross_spread_pct")
                    if opp.get("depth_executable") is True and isinstance(depth_spread, (int, float)) and depth_spread >= min_spread_req:
                        filtered_direct.append(dict(opp))
            filtered_direct.sort(key=lambda o: float(o.get("spread", 0.0)), reverse=True)
            direct_pos = filtered_direct

        stats = {
            "exchanges_count": len(ex_list),
            "exchanges_list": ex_list,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if direct_pos:
            try:
                stats["max_spread"] = max(float(o["spread"]) for o in direct_pos)
            except Exception:
                stats["max_spread"] = 0.0
            try:
                stats["avg_spread"] = sum(float(o["spread"]) for o in direct_pos) / max(1, len(direct_pos))
            except Exception:
                stats["avg_spread"] = 0.0
        else:
            stats["max_spread"] = 0.0
            stats["avg_spread"] = 0.0

        snapshot_age_sec = int(max(0.0, time.time() - snap_ts)) if snap_ts > 0 else None
        try:
            stale_after = float(bot_instance.config.get("monitor_interval", 60) or 60) * 2.0
        except Exception:
            stale_after = 120.0

        return jsonify({
            "success": True,
            "symbol": sym,
            "prices_by_exchange": prices,
            "direct_opportunities": direct_pos,
            "statistics": stats,
            "snapshot_ts": snap_ts,
            "snapshot_age_sec": snapshot_age_sec,
            "snapshot_stale": bool(snapshot_age_sec is not None and snapshot_age_sec >= stale_after),
        })
    except Exception as e:
        logging.error(f"Ошибка в /api/coin_arbitrage: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 200

if __name__ == "__main__":
    app.run(debug=False, host='0.0.0.0', port=8080, use_reloader=False)
