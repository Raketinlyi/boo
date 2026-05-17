"""
Модуль для работы с API DexScreener.
"""

import aiohttp
import logging
import json
from constants import DEXSCREENER_API_BASE, DEFAULT_TIMEOUT

async def get_token_info(session, token_symbol):
    """
    Получает информацию о токене с DexScreener.
    
    Args:
        session (aiohttp.ClientSession): Сессия для HTTP запросов
        token_symbol (str): Символ токена
        
    Returns:
        dict: Информация о токене или None в случае ошибки
    """
    try:
        url = f"{DEXSCREENER_API_BASE}/search?q={token_symbol}"
        logging.info(f"Запрос информации о токене {token_symbol} с DexScreener: {url}")
        
        async with session.get(url, timeout=DEFAULT_TIMEOUT) as response:
            if response.status != 200:
                logging.error(f"Ошибка при запросе к DexScreener: {response.status}")
                return None
            
            data = await response.json()
            
            if not data or "pairs" not in data or not data["pairs"]:
                logging.warning(f"Токен {token_symbol} не найден на DexScreener")
                return None
            
            # Фильтруем пары по символу токена
            pairs = [p for p in data["pairs"] if p["baseToken"]["symbol"].upper() == token_symbol.upper()]
            
            if not pairs:
                logging.warning(f"Токен {token_symbol} не найден среди пар на DexScreener")
                return None
            
            # Сортируем по объему торгов (от большего к меньшему)
            pairs.sort(key=lambda x: float(x.get("volume", {}).get("h24", 0)), reverse=True)
            
            # Берем пару с наибольшим объемом
            best_pair = pairs[0]
            
            result = {
                "symbol": token_symbol,
                "name": best_pair["baseToken"]["name"],
                "address": best_pair["baseToken"]["address"],
                "chain": best_pair["chainId"],
                "price_usd": best_pair["priceUsd"],
                "volume_24h": best_pair["volume"]["h24"],
                "liquidity": best_pair["liquidity"]["usd"],
                "pair": best_pair["pairAddress"],
                "dex": best_pair["dexId"],
                "url": f"https://dexscreener.com/{best_pair['chainId']}/{best_pair['pairAddress']}"
            }
            
            logging.info(f"Получена информация о токене {token_symbol} с DexScreener")
            return result
            
    except Exception as e:
        logging.error(f"Ошибка при получении информации о токене {token_symbol} с DexScreener: {e}")
        return None

async def search_pairs(session, query, limit=10):
    """
    Поиск пар на DexScreener.
    
    Args:
        session (aiohttp.ClientSession): Сессия для HTTP запросов
        query (str): Поисковый запрос
        limit (int): Максимальное количество результатов
        
    Returns:
        list: Список найденных пар или пустой список в случае ошибки
    """
    try:
        url = f"{DEXSCREENER_API_BASE}/search?q={query}"
        logging.info(f"Поиск пар на DexScreener: {url}")
        
        async with session.get(url, timeout=DEFAULT_TIMEOUT) as response:
            if response.status != 200:
                logging.error(f"Ошибка при запросе к DexScreener: {response.status}")
                return []
            
            data = await response.json()
            
            if not data or "pairs" not in data or not data["pairs"]:
                logging.warning(f"Пары не найдены на DexScreener по запросу {query}")
                return []
            
            # Сортируем по объему торгов (от большего к меньшему)
            pairs = data["pairs"]
            pairs.sort(key=lambda x: float(x.get("volume", {}).get("h24", 0)), reverse=True)
            
            # Ограничиваем количество результатов
            pairs = pairs[:limit]
            
            # Форматируем результаты
            results = []
            for pair in pairs:
                results.append({
                    "base_symbol": pair["baseToken"]["symbol"],
                    "base_name": pair["baseToken"]["name"],
                    "quote_symbol": pair["quoteToken"]["symbol"],
                    "chain": pair["chainId"],
                    "price_usd": pair["priceUsd"],
                    "volume_24h": pair["volume"]["h24"],
                    "liquidity": pair["liquidity"]["usd"],
                    "pair": pair["pairAddress"],
                    "dex": pair["dexId"],
                    "url": f"https://dexscreener.com/{pair['chainId']}/{pair['pairAddress']}"
                })
            
            logging.info(f"Найдено {len(results)} пар на DexScreener по запросу {query}")
            return results
            
    except Exception as e:
        logging.error(f"Ошибка при поиске пар на DexScreener по запросу {query}: {e}")
        return []

async def fetch_dex_prices(session, contract_address):
    """
    Получает цены токена на DEX по адресу контракта.
    
    Args:
        session (aiohttp.ClientSession): Сессия для HTTP запросов
        contract_address (str): Адрес контракта токена
        
    Returns:
        dict: Данные о ценах на DEX или словарь с ошибкой
    """
    try:
        url = f"{DEXSCREENER_API_BASE}/tokens/{contract_address}"
        logging.info(f"Запрос цен на DEX для контракта {contract_address}: {url}")
        
        async with session.get(url, timeout=DEFAULT_TIMEOUT) as response:
            if response.status != 200:
                logging.error(f"Ошибка при запросе к DexScreener: {response.status}")
                return {"error": f"HTTP error: {response.status}"}
            
            data = await response.json()
            
            if not data or "pairs" not in data or not data["pairs"]:
                logging.warning(f"Пары не найдены на DexScreener для контракта {contract_address}")
                return {"error": "No pairs found"}
            
            logging.info(f"Получены данные о ценах на DEX для контракта {contract_address}")
            return data
            
    except Exception as e:
        logging.error(f"Ошибка при получении цен на DEX для контракта {contract_address}: {e}")
        return {"error": str(e)}
