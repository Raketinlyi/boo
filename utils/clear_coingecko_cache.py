#!/usr/bin/env python3
"""
Скрипт для очистки кеша CoinGecko.
"""

import os
import sys
import logging
import shutil
from typing import List, Set, Optional

# Настраиваем логгирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

# Константы для кеша
COINGECKO_CACHE_DIR = "data/coingecko_cache"
COINGECKO_LIST_FILE = "data/coingecko_list.json"

def clear_coingecko_cache(keep_list_file: bool = False) -> bool:
    """
    Очищает кеш CoinGecko.
    
    Args:
        keep_list_file: Если True, сохраняет файл списка монет
        
    Returns:
        bool: True если очистка успешна
    """
    try:
        # Удаляем директорию с кешем монет
        if os.path.exists(COINGECKO_CACHE_DIR):
            shutil.rmtree(COINGECKO_CACHE_DIR)
            os.makedirs(COINGECKO_CACHE_DIR, exist_ok=True)
            logging.info(f"Кеш монет CoinGecko успешно очищен ({COINGECKO_CACHE_DIR})")
        else:
            os.makedirs(COINGECKO_CACHE_DIR, exist_ok=True)
            logging.info(f"Директория кеша CoinGecko не существовала, создана новая ({COINGECKO_CACHE_DIR})")
        
        # Удаляем файл списка монет, если нужно
        if not keep_list_file and os.path.exists(COINGECKO_LIST_FILE):
            os.remove(COINGECKO_LIST_FILE)
            logging.info(f"Файл списка монет CoinGecko удален ({COINGECKO_LIST_FILE})")
        
        return True
    except Exception as e:
        logging.error(f"Ошибка при очистке кеша CoinGecko: {e}")
        return False

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Утилита для очистки кеша CoinGecko")
    parser.add_argument("--keep-list", action="store_true", help="Сохранить файл списка монет")
    
    args = parser.parse_args()
    
    if clear_coingecko_cache(keep_list_file=args.keep_list):
        logging.info("Кеш CoinGecko успешно очищен")
    else:
        logging.error("Ошибка при очистке кеша CoinGecko")
        sys.exit(1)
