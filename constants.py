"""
Константы для арбитражного бота.
"""

import os

# Пути к файлам
CONFIG_FILE = "config.json"
COINGECKO_LIST_FILE = os.path.join("data", "coingecko_coins.json")

# Интервалы
COINGECKO_LIST_TTL_SECONDS = 24 * 60 * 60  # 24 часа

# API URLs
COINGECKO_API_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_API_PRO_BASE = "https://pro-api.coingecko.com/api/v3"
DEXSCREENER_API_BASE = "https://api.dexscreener.com/latest/dex"
GECKOTERMINAL_API_BASE = "https://api.geckoterminal.com/api/v2"
JUPITER_LITE_API_BASE = "https://lite-api.jup.ag"
RELAY_API_BASE = "https://api.relay.link"
DEBRIDGE_DLN_API_BASE = "https://dln.debridge.finance/v1.0/dln"
MAYAN_SIA_API_BASE = "https://sia.mayan.finance"
MAYAN_PRICE_API_BASE = "https://price-api.mayan.finance/v3"
MAYAN_SDK_VERSION = "2.5.0"
WORMHOLE_EXECUTOR_API_BASE = "https://executor.labsapis.com"
WORMHOLE_CIRCLE_V2_API_BASE = "https://iris-api.circle.com/v2"
LAYERZERO_TRANSFER_API_BASE = "https://transfer.layerzero-api.com/v1"

# Таймауты по умолчанию (секунды)
DEFAULT_TIMEOUT = 10  # используется в HTTP-запросах, если конкретный таймаут не задан
