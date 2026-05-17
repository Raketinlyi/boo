import logging
from typing import Dict, Set, List, Optional, Any
from aiohttp_retry import RetryClient

from exchanges.base_exchange import Exchange
from config import Config

class SafeTrade(Exchange):
    """
    Адаптер для биржи SafeTrade (Peatio API).
    Публичная документация (пример): https://safe.trade/api/v2/peatio/public/markets
    """
    def __init__(self, config: Config, enabled: bool = True):
        super().__init__(
            "SafeTrade",
            "https://safe.trade/api/v2/peatio/public/markets",
            "https://safe.trade/api/v2/peatio/public/markets/tickers",
            "https://safe.trade/api/v2/peatio/public/markets/{market}/depth?limit=5",
            config,
            enabled
        )
        # Сопоставление базового актива -> id рынка (market) для пары с USDT
        self.symbol_to_market: Dict[str, str] = {}
        # Заголовки, чтобы уменьшить вероятность блокировки
        self.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        })

    async def check_connection(self, session: RetryClient) -> bool:
        """Проверка доступности API, получаем список рынков."""
        try:
            response = await session.get(self.pairs_url, headers=self.headers, timeout=self.timeout)
            if response.status != 200:
                logging.warning(f"[{self.name}] HTTP {response.status} при проверке соединения")
                return False
            try:
                data = await response.json(content_type=None)
                return isinstance(data, list)
            except Exception as e:
                text = await response.text()
                logging.warning(f"[{self.name}] Ошибка разбора JSON при проверке соединения: {e}; text={text[:200]}")
                return False
        except Exception as e:
            logging.error(f"[{self.name}] Ошибка при проверке соединения: {e}")
            return False

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        """
        Получаем список базовых активов для рынков с котировкой USDT.
        Возвращаем множество базовых символов (например, {"BTC", "ETH"}).
        """
        data = await self._make_request(session, self.pairs_url)
        pairs: Set[str] = set()
        self.symbol_to_market.clear()

        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                # Пытаемся получить base и quote в разных вариантах ключей (зависит от конфигурации Peatio)
                base = item.get("base_currency") or item.get("base_unit") or item.get("base")
                quote = item.get("quote_currency") or item.get("quote_unit") or item.get("quote")
                market_id = item.get("id") or item.get("name") or item.get("slug")

                if isinstance(base, str) and isinstance(quote, str) and quote.upper() == "USDT":
                    base_u = base.upper()
                    pairs.add(base_u)
                    if isinstance(market_id, str):
                        self.symbol_to_market[base_u] = market_id
                    else:
                        # Пытаемся угадать id рынка в формате "base_quote"
                        self.symbol_to_market[base_u] = f"{base.lower()}_{quote.lower()}"
        else:
            logging.warning(f"[{self.name}] Неожиданный формат ответа для рынков: {type(data)}")

        logging.info(f"Получено {len(pairs)} пар для {self.name}")
        self.available_pairs = pairs
        return pairs

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        """SafeTrade не предоставляет удобный батч-тикер для наших нужд, используйте книги ордеров."""
        return {}

    def _format_symbol_for_orderbook(self, symbol: str) -> Optional[str]:
        """Возвращает id рынка для depth-запроса по базовому символу."""
        if not symbol:
            return None
        market = self.symbol_to_market.get(symbol.upper())
        if market:
            return market
        # Фоллбэк: id может быть в формате "base_quote"
        return f"{symbol.lower()}_usdt"

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        """
        Получаем верхние уровни книги ордеров для списка базовых символов.
        Возвращает {symbol: {bid, ask, bid_volume, ask_volume}}
        """
        result: Dict[str, Dict[str, float]] = {}
        for symbol in symbols:
            try:
                market_id = self._format_symbol_for_orderbook(symbol)
                if not market_id:
                    continue
                url = self.orderbook_url.format(market=market_id)
                data = await self._make_request(session, url)

                # Если depth не вернул нужную структуру, пробуем order-book
                if not isinstance(data, dict) or ("bids" not in data and "asks" not in data):
                    alt_url = f"https://safe.trade/api/v2/peatio/public/markets/{market_id}/order-book"
                    data = await self._make_request(session, alt_url)

                if not isinstance(data, dict):
                    continue

                bids = data.get("bids") or []
                asks = data.get("asks") or []

                # В Peatio глубина обычно возвращает список [price, volume]
                top_bid = None
                top_ask = None
                if isinstance(bids, list) and bids:
                    try:
                        # bids могут быть отсортированы от высокой к низкой
                        b0 = bids[0]
                        top_bid = (float(b0[0]), float(b0[1])) if isinstance(b0, (list, tuple)) and len(b0) >= 2 else None
                    except Exception:
                        top_bid = None
                if isinstance(asks, list) and asks:
                    try:
                        a0 = asks[0]
                        top_ask = (float(a0[0]), float(a0[1])) if isinstance(a0, (list, tuple)) and len(a0) >= 2 else None
                    except Exception:
                        top_ask = None

                if top_bid and top_ask:
                    result[symbol] = {
                        "bid": top_bid[0],
                        "ask": top_ask[0],
                        "bid_volume": top_bid[1],
                        "ask_volume": top_ask[1]
                    }
            except Exception as e:
                logging.error(f"[{self.name}] Ошибка получения стакана для {symbol}: {e}")
        return result