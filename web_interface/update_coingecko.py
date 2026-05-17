"""
Модуль для обновления списка монет CoinGecko.
Вспомогательный скрипт для обхода проблем с syntax.py
"""

import logging
import asyncio
import threading
import traceback
import time

def update_coingecko_list_async(bot_instance):
    """Асинхронно обновляет список монет CoinGecko."""
    if not bot_instance:
        logging.error("update_coingecko_list_async: Бот не инициализирован")
        return False
    
    try:
        if not hasattr(bot_instance, 'coingecko'):
            # Если объекта нет, создаем его
            from utils.coingecko import CoinGecko
            bot_instance.coingecko = CoinGecko(bot_instance.config)
            logging.info("CoinGecko клиент создан")
        
        # Запускаем асинхронное обновление списка в отдельном потоке
        def update_list_thread():
            import asyncio
            loop = None
            try:
                if not hasattr(bot_instance, 'loop') or not bot_instance.loop:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    new_loop_created = True
                else:
                    loop = bot_instance.loop
                    new_loop_created = False
                    
                # Принудительно обновляем список монет
                logging.info("Запуск обновления списка монет CoinGecko...")
                if new_loop_created:
                    # Если создали новый цикл, выполняем задачу напрямую
                    task = (getattr(bot_instance.coingecko, 'update_coins_list', None) or getattr(bot_instance.coingecko, '_update_coins_list'))(force=True)
                    success = loop.run_until_complete(task)
                else:
                    # Если используем существующий цикл, создаем задачу через run_coroutine_threadsafe
                    future = asyncio.run_coroutine_threadsafe((getattr(bot_instance.coingecko, 'update_coins_list', None) or getattr(bot_instance.coingecko, '_update_coins_list'))(force=True), loop)
                    success = future.result(timeout=30)  # Ждем результат с таймаутом
                
                if success:
                    logging.info(f"Список монет CoinGecko обновлен успешно ({len(bot_instance.coingecko.coins_list)} монет)")
                else:
                    logging.error("Не удалось обновить список монет CoinGecko")
                    
                # Для надежности еще раз загружаем из кэша
                bot_instance.coingecko._load_list_from_cache()
                logging.info(f"Статус после обновления: {len(bot_instance.coingecko.coins_list)} монет, "
                            f"{len(bot_instance.coingecko.coins_list_by_symbol)} символов, "
                            f"{len(bot_instance.coingecko.coins_list_by_id)} ID")
                
                if new_loop_created and loop and loop.is_running():
                    loop.stop()
                    loop.close()
            except Exception as e:
                logging.error(f"Ошибка при обновлении списка монет CoinGecko: {e}\n{traceback.format_exc()}")
                if loop and loop.is_running():
                    try:
                        loop.stop()
                        loop.close()
                    except:
                        pass
                
        # Запускаем поток обновления
        update_thread = threading.Thread(target=update_list_thread, daemon=True)
        update_thread.start()
        return True
    except Exception as e:
        logging.error(f"Ошибка при запуске обновления списка монет CoinGecko: {e}\n{traceback.format_exc()}")
        return False
