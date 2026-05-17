from .base_exchange import Exchange
from .gateio import GateIO
from .mexc import Mexc
from .coinex import CoinEx
from .bybit import Bybit
from .kucoin import KuCoin
from .bingx import BingX
from .safetrade import SafeTrade
from .nonkyc import NonKYC
from .okx import OKX
from .bitget import Bitget
from .binanceus import BinanceUS
from .krakenpro import KrakenPro
from .pionexus import PionexUS
from .lbank import LBank
from .binancealpha import BinanceAlphaManual

# Остальные биржи - временные заглушки для совместимости с калькулятором.
# Bitget импортируется выше как полноценный класс; здесь у нас DummyExchange
# только для совсем старых имён (Biconomy/BinanceUS/HTX), которые раньше
# упоминались в калькуляторе. Bitrue/TradeOgre убраны как закрывшиеся биржи.
class DummyExchange(Exchange):
    def __init__(self, config, enabled=True):
        super().__init__("Dummy", "", "", "", config, enabled=False)
    async def get_all_pairs(self, session): return set()
    async def get_all_tickers(self, session): return {}
    async def get_order_books(self, session, symbols): return {}

Biconomy = HTX = DummyExchange

__all__ = [
    'Exchange', 'GateIO', 'Mexc', 'CoinEx', 'Bybit', 'KuCoin',
    'BingX', 'SafeTrade', 'NonKYC', 'OKX',
    'Biconomy', 'BinanceUS', 'BinanceAlphaManual', 'KrakenPro', 'PionexUS', 'LBank', 'Bitget', 'HTX'
]