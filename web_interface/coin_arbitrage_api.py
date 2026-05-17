"""
Module for finding arbitrage opportunities for a specific coin on all exchanges.

This file contains the API endpoint for getting all possible arbitrage pairs
for the specified coin between all available exchanges.
"""

import logging
import traceback
import asyncio
from datetime import datetime
from flask import jsonify

# Добавляем API-эндпоинт в Flask приложение
def add_coin_arbitrage_api(app, bot_instance):
    """
    Adds API endpoint for finding arbitrage for a specific coin.
    
    Args:
        app: Flask application
        bot_instance: ArbitrageBot instance
    """
    @app.route('/api/coin_arbitrage/<symbol>')
    def get_coin_arbitrage_opportunities(symbol):
        """API for getting all arbitrage opportunities for a specific coin on all exchanges."""
        if not bot_instance:
            return jsonify({"success": False, "error": "Бот не инициализирован"})
        
        try:
            # Получаем все доступные биржи
            enabled_exchanges = []
            if hasattr(bot_instance, 'calc'):
                enabled_exchanges = [ex.name for ex in bot_instance.calc.exchanges if ex.enabled]
            
            logging.info(f"Поиск арбитража для монеты {symbol} на {len(enabled_exchanges)} биржах")
            
            # Используем Exchange API для получения актуальных цен
            prices = {}
            volume_data = {}
            exchanges_with_symbol = []
            
            # Асинхронно получаем цены с каждой биржи
            async def fetch_exchange_data():
                tasks = []
                for ex_name in enabled_exchanges:
                    exchange = next((e for e in bot_instance.calc.exchanges if e.name == ex_name), None)
                    if not exchange:
                        continue
                        
                    # Проверяем наличие символа на бирже
                    if symbol.upper() in exchange.pairs:
                        exchanges_with_symbol.append(ex_name)
                        # Создаем задачу для получения данных с биржи
                        tasks.append(exchange.fetch_ticker_data(symbol.upper()))
                
                # Выполняем все запросы параллельно
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    # Обрабатываем результаты
                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            logging.error(f"Ошибка при получении данных с биржи {exchanges_with_symbol[i]}: {result}")
                        else:
                            ex_name = exchanges_with_symbol[i]
                            if result and isinstance(result, dict):
                                price = result.get('price', 0)
                                if price > 0:
                                    prices[ex_name] = price
                                    # Собираем данные об объемах, если они доступны
                                    volume = result.get('volume', 0)
                                    if volume > 0:
                                        volume_data[ex_name] = volume
            
            # Выполняем асинхронный код
            async def main():
                await fetch_exchange_data()
            
            # Запускаем асинхронные запросы
            if hasattr(bot_instance, 'loop') and bot_instance.loop:
                bot_instance.loop.run_until_complete(main())
            else:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(main())
                loop.close()
            
            # Теперь находим все возможные арбитражные пары
            arbitrage_pairs = []
            exchanges_list = list(prices.keys())
            
            for i in range(len(exchanges_list)):
                buy_exchange = exchanges_list[i]
                buy_price = prices[buy_exchange]
                
                for j in range(len(exchanges_list)):
                    if i == j:
                        continue
                        
                    sell_exchange = exchanges_list[j]
                    sell_price = prices[sell_exchange]
                    
                    # Рассчитываем спред
                    if buy_price > 0:  # Защита от деления на ноль
                        spread = ((sell_price - buy_price) / buy_price) * 100
                        
                        # Добавляем в список возможностей
                        arbitrage_pairs.append({
                            "symbol": symbol.upper(),
                            "buy_exchange": buy_exchange,
                            "sell_exchange": sell_exchange,
                            "buy_price": buy_price,
                            "sell_price": sell_price,
                            "spread": spread,
                            "buy_volume": volume_data.get(buy_exchange, 0),
                            "sell_volume": volume_data.get(sell_exchange, 0),
                            "timestamp": datetime.now().isoformat()
                        })
            
            # Сортируем по спреду (от высокого к низкому)
            arbitrage_pairs.sort(key=lambda x: x["spread"], reverse=True)
            
            # Фильтруем только положительный спред
            positive_opportunities = [op for op in arbitrage_pairs if op["spread"] > 0]
            
            # Добавляем статистику
            stats = {
                "exchanges_count": len(prices),
                "exchanges_with_symbol": len(exchanges_with_symbol),
                "opportunities_count": len(positive_opportunities),
                "max_spread": max([op["spread"] for op in positive_opportunities]) if positive_opportunities else 0,
                "min_spread": min([op["spread"] for op in positive_opportunities]) if positive_opportunities else 0,
                "avg_spread": sum([op["spread"] for op in positive_opportunities]) / len(positive_opportunities) if positive_opportunities else 0,
                "exchanges_list": list(prices.keys()),
                "timestamp": datetime.now().isoformat()
            }
            
            return jsonify({
                "success": True,
                "symbol": symbol.upper(),
                "opportunities": positive_opportunities,
                "stats": stats
            })
            
        except Exception as e:
            logging.error(f"Ошибка при поиске арбитражных возможностей для монеты {symbol}: {str(e)}\n{traceback.format_exc()}")
            return jsonify({
                "success": False,
                "error": str(e),
                "message": f"Не удалось найти арбитражные возможности для монеты {symbol}"
            })
