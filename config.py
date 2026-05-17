"""
Модуль конфигурации для арбитражного бота.
"""

import json
import os
import logging
import traceback
from typing import Any, Dict, Optional
import time


def _load_local_api_keys() -> Dict[str, Any]:
    """Load read-only exchange keys from api_keys_PRIVATE.json if it exists.

    These keys are used only for account/asset status helpers. The bot scanner
    still works from public market-data endpoints.
    """
    path = os.path.join(os.path.dirname(__file__), "api_keys_PRIVATE.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        logging.warning("Failed to load api_keys_PRIVATE.json: %s", exc)
    return {}

# Значения по умолчанию
DEFAULT_CONFIG = {
    "min_spread": 0.5,
    "max_spread": 50.0,
    "pairs_update_interval": 3, # в минутах
    "monitor_interval": 30, # в секундах
    "price_timeout": 10,
    "stale_data_ttl_sec": 180,
    # If an exchange is slow/unresponsive, don't let it stall the whole iteration.
    # Lowered from 8.0 to 5.0: live prices now flow via WebSocket (ws_server),
    # so the REST poll is only a warm-up / fallback path. A slow exchange
    # shouldn't delay the whole batch.
    "tickers_per_exchange_timeout_sec": 5.0,
    # Penalize stale data in ranking (optional, best-effort). Off by default.
    "stale_rank_penalty_enabled": False,
    "stale_rank_penalty_grace_sec": 10.0,
    "stale_rank_penalty_per_min_pct": 0.2,
    "stale_rank_hide_after_sec": 0.0,
    # Optional orderbook refinement: expensive, disabled by default.
    "use_orderbooks": True,
    # OKX last-trade prices can create phantom spreads on thin/stale pairs.
    # Validate OKX opportunities with best bid/ask before showing them.
    "okx_require_orderbook_validation": True,
    "okx_validation_top_symbols": 80,
    "okx_validation_per_exchange_timeout_sec": 4.0,
    # When enabled, fetch orderbooks only for the top-N symbols (by spread) to avoid rate limits.
    "orderbooks_refine_top_symbols": 5,
    # Best-effort timeout per exchange when fetching orderbooks for a symbol subset.
    # Lowered from 10.0 to 6.0 — matches the p95 response time of CEX public
    # orderbook endpoints; anything longer is almost always network trouble.
    "orderbooks_per_exchange_timeout_sec": 6.0,
    # Modal "Инфо" — deposit/withdraw flags. MUST stay short: exchanges toggle
    # these during maintenance. Anything above ~15 min risks showing "open"
    # when the wallet is already frozen — an actively harmful stale read for
    # an arbitrage bot. Prefetch loop refreshes every ~15s per batch.
    "asset_status_ttl_sec": 600,
    # Inter-chain scanner cache (CEX↔DEX, межсетевой).
    # Lowered 90→60s so UI sees fresher bridge quotes.
    "cex_dex_scan_ttl_sec": 60,
    # DEX quote cache: 30s (was 45). DEX prices can gap in seconds.
    "dex_quote_ttl_sec": 30,
    # CEX→DEX discovery: first use exchange contracts and CoinGecko platforms;
    # GeckoTerminal is secondary liquidity confirmation; DexScreener is OFF by default.

    # Manual-only/read-only venues: show opportunities, but do not assume API trading.
    "manual_market_sources": {
        "binance_alpha_enabled": True,
        "min_alpha_liquidity_usd": 0.0,
        "max_alpha_symbol_candidates": 1
    },
    "dex_contract_discovery": {
        "enabled": True,
        "use_exchange_contracts": True,
        "use_coingecko_platforms": True,
        "use_geckoterminal_search": True,
        "use_dexscreener": False,
        "min_liquidity_usd": 5000.0,
        "min_volume_24h_usd": 1000.0,
        "max_candidates_per_asset": 6,
    },
    # Standalone WebSocket-first server (run_ws_server.py). REST scanner remains fallback.
    "ws_server_port": 8090,
    "ws_gate_enabled": True,
    "ws_gate_max_symbols": 3000,
    "ws_kucoin_max_symbols": 3000,
    "ws_okx_max_symbols": 3000,
    "ws_bybit_max_symbols": 3000,
    "ws_bitget_max_symbols": 3000,
    "ws_binanceus_max_symbols": 1500,
    "ws_krakenpro_max_symbols": 300,
    "ws_pionexus_max_symbols": 300,
    "ws_lbank_max_symbols": 200,
    "lbank_base_url": "https://api.lbkex.com",
    "lbank_orderbook_concurrency": 5,
    "pionexus_base_url": "https://api.pionex.com",
    "ws_rest_fallback_interval_sec": 30.0,
    "ws_quote_ttl_sec": 10.0,
    "ws_use_for_ui": True,
    "ws_require_top_liquidity": False,
    "ws_ui_timeout_sec": 1.5,
    "ws_ui_auto_start": True,
    "alpha_manual_price_match_pct": 30.0,
    "alpha_manual_min_cex_sources": 1,
    "alpha_manual_require_price_match": True,

    # Kraken ↔ KyberSwap separate scanner. Runs only when the UI tab is opened.
    "kraken_kyber_enabled": True,
    "kraken_kyber_min_spread": 0.5,
    "kraken_kyber_asset_limit": 120,
    "kraken_kyber_notional_usd": 10.0,
    "kraken_kyber_price_match_pct": 20.0,
    "kraken_kyber_require_kraken_on_coingecko": True,
    "kraken_kyber_max_candidates_per_symbol": 8,
    "kraken_kyber_max_assets_parallel": 4,
    "kraken_kyber_cache_ttl_sec": 30,
    "kraken_kyber_contract_cache_ttl_sec": 86400,
    "kraken_kyber_route_cache_ttl_sec": 8,
    "kraken_kyber_kraken_orderbook_ttl_sec": 5,
    "kraken_kyber_kraken_orderbook_count": 100,
    "kraken_kyber_kraken_depth_min_fill_pct": 95.0,
    "kraken_kyber_proxy_url": "",
    "kraken_kyber_kraken_proxy_url": "",
    "kraken_kyber_coingecko_proxy_url": "",
    "kraken_kyber_kyber_proxy_url": "",
    "kraken_kyber_use_contract_index": True,
    "kraken_kyber_index_refresh_sec": 18000,
    "kraken_kyber_index_asset_limit": 600,
    "kraken_kyber_coingecko_delay_sec": 2.2,
    "kraken_kyber_kraken_ticker_ttl_sec": 5,
    "kraken_kyber_chains": ["ethereum", "bsc", "polygon", "arbitrum", "base", "optimism", "avalanche"],
    "alpha_manual_reject_duplicate_symbols": True,
    "ws_adapter_startup_stagger_sec": 0.18,
    "ws_adapter_reconnect_base_delay_sec": 3.0,
    "web_port": 8080,
    "auto_restart_on_zero_opportunities": False,
    "max_zero_opportunities_before_restart": 0,
    # Мягкое восстановление после сна/обрывов сети: пересоздаём HTTP-сессию, если подряд нет данных от бирж
    "soft_recover_zero_tickers_streak": 3,
    "soft_recover_cooldown_sec": 60,
    "enabled_exchanges": [
        # Включены биржи, у которых есть публичный market-data API для сканера.
        # Binance Alpha (manual) — только сигнал/стакан, без API-торговли.
        "Gate.io", "MEXC", "CoinEx", "Bybit", "OKX", "KuCoin", "Bitget",
        "BingX", "SafeTrade", "NonKYC", "Binance.US", "Binance Alpha (manual)",
        "Kraken Pro", "Pionex.US", "LBank"
    ],
    "fees": {  # Комиссии бирж в процентах
        "binance": 0.1,
        "kucoin": 0.1,
        "gate.io": 0.2,
        "mexc": 0.2,
        "coinex": 0.2,
        "bybit": 0.1,
        "okx": 0.1,
        "bitget": 0.1,
        "bingx": 0.1,
        "binance.us": 0.1,
        "Binance Alpha (manual)": 0.0,
        "binance alpha (manual)": 0.0,
        "kraken pro": 0.26,
        "pionex.us": 0.05,
        "lbank": 0.1
    },
    "interchain_bridge_provider_priority": ["wormhole", "layerzero", "mayan", "relay", "bungee", "squid", "debridge", "across", "skip"],
    "interchain_bridge_provider_blacklist": [],
    "interchain_live_bridge_provider_priority": ["mayan", "wormhole", "layerzero", "relay", "debridge"],
    "interchain_live_bridge_provider_blacklist": [],
    "interchain_parallel_assets": 4,
    "interchain_parallel_assets_quick": 3,
    "coingecko_api_key": None,  # Ключ CoinGecko Pro API
    "coinmarketcap_api_key": None,  # Ключ CoinMarketCap API
    "api_source_settings": {
        "default_source": "coingecko",
        "fallback_source": "coinmarketcap",
        "auto_switch": True,
        "switch_cooldown": 300,  # 5 минут
        "max_failures": 5  # Количество ошибок до переключения
    },
    "api_settings": {
        "coingecko": {
            "use_pro": True,  # Использовать ли Pro API
            "rate_limit": 24,  # Запросов в минуту для публичного API (не более 24/мин)
            "pro_rate_limit": 100,  # Запросов в минуту для Pro API
            "retry_delay": 1.0,  # Задержка между запросами в секундах
            "cache_ttl": 300  # Время жизни кэша в секундах
        },
        "coinmarketcap": {
            "enabled": False,  # Включен ли CMC API
            "rate_limit": 30,  # Запросов в минуту
            "retry_delay": 1.0,
            "cache_ttl": 300
        }
    },
    # UI: отображение дополнительных колонок в таблице возможностей
    "ui_show_momentum_1m": True,
    "ui_show_momentum_15m": True,
    "ui_show_heat": True,
    "ui_show_dispersion": True,
    "ui_group_by_liquidity": False,
    "ui_show_direction": False,
    "ui_arb_filter_liquidity": True,
    # UI: порог минимального профита для popover-фильтра (в процентах)
    "ui_popover_min_profit_pct": 0.0,
    "arb_min_notional_usd": 10.0,
    "ui_arb_top_liquidity_n": 0,

    # Настройки эвристики направления и отображения
    "direction": {
        # Пороги для классификации направления по score (модуль)
        "dir_up_thresh": 0.25,
        "dir_down_thresh": 0.25,
        # Порог сильного ап-сигнала для обучения (используется в псевдо-лейблинге)
        "dir_up_strong": 0.25,
        # Использовать ли сигнал направления при формировании псевдо-лейблов
        "use_direction_in_label": True,
        # Веса сигналов при вычислении score (нормализованные компоненты)
        "weights": {
            "m1": 0.5,
            "m5": 0.7,
            "spike": 0.5,
            "slope": 0.3
        },
        # Пороги силы для цветовых бейджей по |score| (две границы → три уровня)
        "strength_thresholds": [0.33, 0.66],
        # Цвета Bootstrap для уровней силы (низкий/средний/высокий)
        "colors": ["secondary", "warning", "success"],
        # Минимальная уверенность для отображения направления в UI
        "conf_min_to_show": 0.4,
        # Доп. бусты уверенности от объёмных всплесков и "тепла"
        "conf_boosts": {"vs1": 0.15, "vs5": 0.15, "heat": 0.1},
        # Порог уверенности для использования направления в псевдо-лейбле обучения
        "conf_for_label": 0.6,
        # Порог высокой уверенности, при котором усиливаем вес примера при обучении
        "high_conf_threshold": 0.8,
        # Множитель веса примера при высокой уверенности (например 1.2–1.4)
        "high_conf_weight_boost": 1.3
    },
    # Настройки обновления фичей CoinGecko
    "cg_feature_ttl_sec": 600,                 # TTL кэша фич (сек)
    "cg_feature_budget_per_iter": 3,          # Лимит обновлений за итерацию
    "coingecko_refresh_interval_min": 15,     # Период фонового освежения (мин)
    "coingecko_refresh_symbol_limit": 30,     # Макс. монет на освежение за проход

    # Пакетное обновление общих метрик CoinGecko (рынки): раз в N минут, батчами до 250 монет
    "cg_batch_refresh_enabled": True,          # Включить пакетное обновление markets
    "cg_batch_interval_min": 5,               # Период пакетного обновления (мин)
    "cg_batch_size": 250,                    # Размер батча (макс 250 id за запрос)

    # Долговременная память по CoinGecko markets
    "cg_memory": {
        "persist_enabled": True,             # Писать кэш и историю markets в SQLite
        "bootstrap_hours": 24,               # При старте восстанавливать recent-символы за N часов
        "history_retention_days": 14         # Сколько дней хранить историю markets
    },

    # Retention / storage
    "save_opportunities_to_db": True,
    "arbitrage_history_retention_days": 0,
    "log_retention_days": 1,
    "fs_retention_days": 0
}

class Config:
    """
    Класс для работы с конфигурацией бота.
    """
    
    def __init__(self, config_file="config.json"):
        """
        Инициализирует объект конфигурации.
        
        Args:
            config_file: Путь к файлу конфигурации
        """
        self.config_file = config_file
        self.config = {}
        self.load()
        
        # Инициализация состояния API-ключей
        self._api_states = {
            "coingecko": {
                "is_invalid": False,
                "last_check": 0,
                "check_interval": 300,  # 5 минут
                "remaining_calls": None,
                "reset_time": None
            },
            "coinmarketcap": {
                "is_invalid": False,
                "last_check": 0,
                "check_interval": 300,
                "remaining_calls": None,
                "reset_time": None
            }
        }

    def load(self):
        """Загружает конфигурацию из файла."""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
                self.validate() # Проверяем и дополняем загруженную конфигурацию
                self.config["api_keys"] = _load_local_api_keys()
            else:
                self.config = DEFAULT_CONFIG.copy()
                self.config["api_keys"] = _load_local_api_keys()
                self.save()  # Создаем файл с конфигурацией по умолчанию
        except Exception as e:
            logging.error(f"Ошибка загрузки конфигурации: {e}")
            self.config = DEFAULT_CONFIG.copy()
            self.config["api_keys"] = _load_local_api_keys()

    def validate(self):
        """Проверяет корректность значений в конфигурации."""
        # Базовая валидация
        for key, default_value in DEFAULT_CONFIG.items():
            if key not in self.config:
                logging.warning(f"Некорректное значение {key} в config.json. Используется значение по умолчанию.")
                self.config[key] = default_value
                continue

            value = self.config[key]

            # Послабления по типам для некоторых ключей
            try:
                if default_value is None:
                    # Для API ключей разрешаем либо None, либо строку
                    if key in ("coingecko_api_key", "coinmarketcap_api_key"):
                        if value is not None and not isinstance(value, str):
                            logging.warning(f"Некорректное значение {key} в config.json. Используется значение по умолчанию.")
                            self.config[key] = default_value
                    # Для прочих ключей с None по умолчанию не навязываем тип строго
                    # (оставляем пользовательское значение как есть)
                else:
                    # Числовая совместимость: допускаем int<->float
                    if isinstance(default_value, (int, float)):
                        if not isinstance(value, (int, float)):
                            logging.warning(f"Некорректное значение {key} в config.json. Используется значение по умолчанию.")
                            self.config[key] = default_value
                    else:
                        # Строгая проверка типов для остальных случаев
                        if not isinstance(value, type(default_value)):
                            logging.warning(f"Некорректное значение {key} в config.json. Используется значение по умолчанию.")
                            self.config[key] = default_value
            except Exception:
                logging.warning(f"Некорректное значение {key} в config.json. Используется значение по умолчанию.")
                self.config[key] = default_value

        # Валидация API настроек
        if "api_settings" not in self.config:
            self.config["api_settings"] = DEFAULT_CONFIG["api_settings"]
        else:
            for api_name, default_settings in DEFAULT_CONFIG["api_settings"].items():
                if api_name not in self.config["api_settings"]:
                    self.config["api_settings"][api_name] = default_settings
                else:
                    for setting, default_value in default_settings.items():
                        if setting not in self.config["api_settings"][api_name]:
                            self.config["api_settings"][api_name][setting] = default_value

    def save(self):
        """Сохраняет конфигурацию в файл."""
        try:
            os.makedirs(os.path.dirname(self.config_file) or ".", exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            logging.info(f"Конфигурация сохранена в {self.config_file}")
        except Exception as e:
            logging.error(f"Ошибка сохранения конфигурации в файл {self.config_file}: {e}\n{traceback.format_exc()}")

    def get(self, key: str, default: Any = None) -> Any:
        """
        Получает значение из конфигурации.
        
        Args:
            key: Ключ
            default: Значение по умолчанию, если ключ не найден
            
        Returns:
            Значение из конфигурации или значение по умолчанию
        """
        # Сначала ищем в текущей конфигурации, потом в дефолтной, потом возвращаем default
        if key in self.config:
            return self.config[key]
        return DEFAULT_CONFIG.get(key, default)

    def set(self, key: str, value: Any):
        """
        Устанавливает значение в конфигурации.
        
        Args:
            key: Ключ
            value: Значение
        """
        self.config[key] = value
        self.save()

    def remove(self, key: str):
        """
        Удаляет значение из конфигурации.
        
        Args:
            key: Ключ
        """
        if key in self.config:
            del self.config[key]
            self.save()

    def mark_api_key_invalid(self, api_name: str) -> None:
        """
        Помечает API-ключ как невалидный.
        
        Args:
            api_name: Имя API ('coingecko' или 'coinmarketcap')
        """
        if api_name in self._api_states:
            self._api_states[api_name]["is_invalid"] = True
            self._api_states[api_name]["last_check"] = time.time()
            logging.warning(f"API-ключ {api_name} помечен как невалидный. Будет использоваться публичное API.")

    def update_api_limits(self, api_name: str, remaining_calls: Optional[int] = None, reset_time: Optional[float] = None):
        """
        Обновляет информацию о лимитах API.
        
        Args:
            api_name: Имя API
            remaining_calls: Оставшееся количество вызовов
            reset_time: Время сброса лимитов
        """
        if api_name in self._api_states:
            if remaining_calls is not None:
                self._api_states[api_name]["remaining_calls"] = remaining_calls
            if reset_time is not None:
                self._api_states[api_name]["reset_time"] = reset_time

    def is_api_key_available(self, api_name: str) -> bool:
        """
        Проверяет доступность API-ключа.
        
        Args:
            api_name: Имя API
            
        Returns:
            bool: True если ключ доступен
        """
        if api_name not in self._api_states:
            return False
            
        api_key = self.get(f"{api_name}_api_key")
        if not api_key:
            return False
            
        state = self._api_states[api_name]
        current_time = time.time()
        
        # Если ключ был помечен как невалидный
        if state["is_invalid"]:
            # Проверяем, прошло ли достаточно времени для повторной попытки
            if current_time - state["last_check"] > state["check_interval"]:
                state["is_invalid"] = False
                state["last_check"] = current_time
                logging.info(f"Повторная попытка использования API-ключа {api_name}.")
                return True
            return False
            
        # Проверяем лимиты
        if state["remaining_calls"] is not None and state["reset_time"] is not None:
            if state["remaining_calls"] <= 0 and current_time < state["reset_time"]:
                logging.warning(f"Достигнут лимит вызовов API {api_name}. Сброс через {state['reset_time'] - current_time:.0f} секунд.")
                return False
                
        return True

    def get_api_settings(self, api_name: str) -> Dict:
        """
        Получает настройки для конкретного API.
        
        Args:
            api_name: Имя API
            
        Returns:
            Dict: Настройки API
        """
        return self.config.get("api_settings", {}).get(api_name, DEFAULT_CONFIG["api_settings"][api_name])

    def get_exchange_fee(self, exchange: str, default_fee: float = 0.1) -> float:
        """
        Получает комиссию для указанной биржи.
        
        Args:
            exchange: Название биржи
            default_fee: Комиссия по умолчанию в процентах
            
        Returns:
            Комиссия биржи в процентах
        """
        # Проверяем наличие секции fees в конфигурации
        fees = self.config.get('fees', {})
        normalized_fees = {}
        if isinstance(fees, dict):
            normalized_fees = {
                str(name).strip().lower(): value
                for name, value in fees.items()
            }
        # Получаем комиссию для биржи или используем значение по умолчанию
        return normalized_fees.get(str(exchange).strip().lower(), default_fee)
