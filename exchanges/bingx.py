import logging
import json
import time
import re
from typing import Dict, Set, List
from aiohttp_retry import RetryClient

from exchanges.base_exchange import Exchange
from config import Config

class BingX(Exchange):
    """
    Класс для работы с API биржи BingX.
    """
    
    def __init__(self, config: Config, enabled: bool = True):
        """
        Инициализирует объект биржи BingX.
        
        Args:
            config: Объект конфигурации
            enabled: Флаг, указывающий, включена ли биржа
        """
        # Используем публичный API для спотовой торговли
        self.BASE_URL = "https://open-api.bingx.com"
        super().__init__(
            "BingX",
            f"{self.BASE_URL}/openApi/spot/v1/common/symbols",  # URL для пар (обновлено)
            f"{self.BASE_URL}/openApi/spot/v1/ticker/bookTicker",   # URL для тикеров (обновлено)
            f"{self.BASE_URL}/openApi/spot/v1/market/depth?symbol={{symbol}}&limit=5",
            config, enabled
        )
        # Добавляем специальные заголовки для BingX
        self.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        })
    
    async def check_connection(self, session: RetryClient) -> bool:
        """
        Проверяет соединение с API биржи BingX.
        
        Args:
            session: Сессия HTTP-клиента
            
        Returns:
            True, если соединение успешно, иначе False
        """
        try:
            # Пингуем рабочий публичный эндпоинт (тикер по BTCUSDT)
            url = f"{self.BASE_URL}/openApi/spot/v1/ticker/bookTicker?symbol=BTC-USDT"
            response = await session.get(url, headers=self.headers, timeout=10)
            
            if response.status == 200:
                data = await response.json(content_type=None)
                if isinstance(data, dict) and data.get("code") == 0 and "data" in data:
                    item = data["data"]
                    # API может вернуть либо dict, либо list из одного/нескольких элементов
                    entries = item if isinstance(item, list) else [item]
                    for entry in entries:
                        if isinstance(entry, dict):
                            try:
                                bid_ok = float(entry.get("bidPrice", 0)) > 0
                                ask_ok = float(entry.get("askPrice", 0)) > 0
                            except Exception:
                                bid_ok = False
                                ask_ok = False
                            if bid_ok or ask_ok:
                                return True
            
            logging.warning(f"{self.name}: Ошибка проверки соединения. Код: {response.status}, Ответ: {await response.text()}")
            return False
        except Exception as e:
            logging.error(f"{self.name}: Ошибка при проверке соединения: {e}")
            return False
    
    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        """
        Получает список всех торговых пар с биржи BingX.
        
        Args:
            session: Сессия HTTP-клиента
            
        Returns:
            Множество символов торговых пар
        """
        pairs = set()
        
        # Пробуем получить пары через API
        try:
            # Используем правильный эндпоинт для получения символов
            url = f"{self.BASE_URL}/openApi/spot/v1/common/symbols"
            response = await session.get(url, headers=self.headers, timeout=10)
            
            if response.status == 200:
                data = await response.json(content_type=None)
                
                if isinstance(data, dict) and "code" in data and data["code"] == 0 and "data" in data:
                    symbols_data = data["data"]
                    if isinstance(symbols_data, list):
                        for item in symbols_data:
                            # Форматы могут различаться: либо dict с baseAsset/quoteAsset, либо dict/str с "BTC-USDT" или "BTCUSDT"
                            if isinstance(item, dict):
                                if "baseAsset" in item and "quoteAsset" in item:
                                    if str(item["quoteAsset"]).upper() == "USDT" and (item.get("status") in (None, "TRADING", "Trading")):
                                        base = str(item["baseAsset"]).upper()
                                        pairs.add(f"{base}USDT")
                                elif "symbol" in item:
                                    s = str(item["symbol"]).upper()
                                    if "-" in s:
                                        base, quote = s.split("-", 1)
                                        if quote == "USDT":
                                            pairs.add(f"{base}USDT")
                                    elif s.endswith("USDT"):
                                        pairs.add(s)
                            elif isinstance(item, str):
                                s = item.upper()
                                if "-" in s:
                                    base, quote = s.split("-", 1)
                                    if quote == "USDT":
                                        pairs.add(f"{base}USDT")
                                elif s.endswith("USDT"):
                                    pairs.add(s)
                    logging.info(f"{self.name}: Получено {len(pairs)} пар через API")
                else:
                    logging.warning(f"{self.name}: Неожиданный формат ответа для пар: {data}")
            else:
                logging.warning(f"{self.name}: Ошибка получения пар. Код: {response.status}, Ответ: {await response.text()}")
        except Exception as e:
            logging.error(f"{self.name}: Ошибка при получении пар через API: {e}")
        
        # Если не удалось получить пары через API, используем хардкодированный список
        if not pairs:
            logging.warning(f"{self.name}: Не удалось получить пары через API, используем хардкодированный список")
            popular_coins = [
                "BTC", "ETH", "XRP", "LTC", "BCH", "EOS", "TRX", "ETC", "LINK", "DOT", 
                "ADA", "DOGE", "UNI", "SOL", "MATIC", "AVAX", "SHIB", "NEAR", "ATOM", 
                "FTM", "ALGO", "ICP", "VET", "FIL", "MANA", "SAND", "AXS", "GALA"
            ]
            for coin in popular_coins:
                pairs.add(f"{coin}USDT")
        
        logging.info(f"Получено {len(pairs)} пар для {self.name}")
        self.available_pairs = pairs
        return pairs
    
    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        """
        Получает цены всех торговых пар с биржи BingX.
        
        Args:
            session: Сессия HTTP-клиента
            
        Returns:
            Словарь {символ: цена} для всех торговых пар
        """
        result = {}
        
        # Если нет доступных пар, пытаемся их получить
        if not self.available_pairs:
            await self.get_all_pairs(session)
        
        if not self.available_pairs:
            logging.warning(f"{self.name}: Нет доступных пар для получения тикеров")
            return result
        
        # Пробуем получить все тикеры сразу
        try:
            # Используем правильный эндпоинт для получения тикеров
            url = f"{self.BASE_URL}/openApi/spot/v1/ticker/bookTicker"
            response = await session.get(url, headers=self.headers, timeout=10)
            
            if response.status == 200:
                data = await response.json(content_type=None)
                
                if isinstance(data, dict) and "code" in data and data["code"] == 0 and "data" in data:
                    tickers_data = data["data"]
                    if isinstance(tickers_data, list):
                        for item in tickers_data:
                            if isinstance(item, dict) and "symbol" in item:
                                sym = str(item["symbol"]).upper()
                                base_symbol = None
                                quote_symbol = None
                                if "-" in sym:
                                    base_symbol, quote_symbol = sym.split("-", 1)
                                elif sym.endswith("USDT") and len(sym) > 4:
                                    base_symbol, quote_symbol = sym[:-4], "USDT"

                                if base_symbol and quote_symbol == "USDT":
                                    bid = item.get("bidPrice")
                                    ask = item.get("askPrice")
                                    price_val = None
                                    try:
                                        bid_f = float(bid) if bid is not None else float("nan")
                                        ask_f = float(ask) if ask is not None else float("nan")
                                        if bid_f > 0 and ask_f > 0:
                                            price_val = (bid_f + ask_f) / 2.0
                                        elif ask_f > 0:
                                            price_val = ask_f
                                        elif bid_f > 0:
                                            price_val = bid_f
                                    except Exception:
                                        price_val = None
                                    if price_val and price_val > 0:
                                        result[f"{base_symbol}USDT"] = price_val
                    logging.info(f"{self.name}: Получено {len(result)} тикеров через API")
                else:
                    logging.warning(f"{self.name}: Неожиданный формат ответа для тикеров: {data}")
            else:
                logging.warning(f"{self.name}: Ошибка получения тикеров. Код: {response.status}, Ответ: {await response.text()}")
        except Exception as e:
            logging.error(f"{self.name}: Ошибка при получении тикеров: {e}")
        
        # Если не удалось получить тикеры через общий запрос, пробуем получить по одному
        if not result:
            logging.info(f"{self.name}: Пробуем получить тикеры по одному")
            for symbol in self.available_pairs:
                try:
                    # Используем эндпоинт для получения цены конкретной пары
                    single_ticker_url = f"{self.BASE_URL}/openApi/spot/v1/ticker/bookTicker?symbol={self._format_symbol_for_orderbook(symbol)}"
                    single_response = await session.get(single_ticker_url, headers=self.headers, timeout=10)
                    
                    if single_response.status == 200:
                        single_data = await single_response.json(content_type=None)
                        
                        if isinstance(single_data, dict) and "code" in single_data and single_data["code"] == 0 and "data" in single_data:
                            ticker_data = single_data["data"]
                            if isinstance(ticker_data, dict):
                                bid = ticker_data.get("bidPrice")
                                ask = ticker_data.get("askPrice")
                                price_val = None
                                try:
                                    bid_f = float(bid) if bid is not None else float("nan")
                                    ask_f = float(ask) if ask is not None else float("nan")
                                    if bid_f > 0 and ask_f > 0:
                                        price_val = (bid_f + ask_f) / 2.0
                                    elif ask_f > 0:
                                        price_val = ask_f
                                    elif bid_f > 0:
                                        price_val = bid_f
                                except Exception:
                                    price_val = None
                                if price_val and price_val > 0:
                                    result[symbol] = price_val
                except Exception as e:
                    logging.error(f"{self.name}: Ошибка при получении тикера для {symbol}: {e}")
        
        logging.info(f"Получено {len(result)} тикеров для {self.name}")
        return result
    
    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        result = {}
        import asyncio
        semaphore = asyncio.Semaphore(8)

        async def _fetch_one(symbol: str) -> None:
            async with semaphore:
                formatted_symbol = self._format_symbol_for_orderbook(symbol)
                url = f"{self.BASE_URL}/openApi/spot/v1/market/depth?symbol={formatted_symbol}&limit=20"
                try:
                    response = await session.get(url, headers=self.headers, timeout=10)
                    if response.status != 200:
                        return
                    data = await response.json(content_type=None)
                    if not isinstance(data, dict) or data.get("code") != 0 or "data" not in data:
                        return
                    orderbook = data["data"]
                    if not isinstance(orderbook, dict):
                        return
                    bids = orderbook.get("bids")
                    asks = orderbook.get("asks")
                    if not bids or not asks or not isinstance(bids, list) or not isinstance(asks, list):
                        return
                    bid_price = float(bids[0][0])
                    bid_volume = float(bids[0][1])
                    ask_price = float(asks[0][0])
                    ask_volume = float(asks[0][1])
                    if bid_price > 0 and ask_price > 0:
                        result[symbol] = {
                            "bid": bid_price,
                            "ask": ask_price,
                            "bid_volume": bid_volume,
                            "ask_volume": ask_volume,
                            "bids": bids,
                            "asks": asks,
                        }
                except (IndexError, ValueError, TypeError) as e:
                    logging.warning(f"{self.name}: Error parsing orderbook for {symbol}: {e}")
                except Exception as e:
                    logging.error(f"{self.name}: Error fetching orderbook for {symbol}: {e}")

        await asyncio.gather(*[_fetch_one(s) for s in symbols])
        return result
    
    def _format_symbol_for_orderbook(self, symbol: str) -> str:
        """
        Форматирует символ для использования в запросе книги ордеров BingX.
        
        Args:
            symbol: Символ торговой пары
            
        Returns:
            Отформатированный символ
        """
        s = str(symbol or "").upper().strip()
        if not s:
            return s
        # Already formatted like BTC-USDT
        if "-" in s:
            return s
        # Convert BTCUSDT -> BTC-USDT
        if s.endswith("USDT") and len(s) > 4:
            base = s[:-4]
            return f"{base}-USDT"
        return s
