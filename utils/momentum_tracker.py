from collections import defaultdict, deque
from statistics import median
from typing import Deque, Dict, Iterable, List, Optional, Tuple
import time


class MomentumTracker:
    """
    Легковесный трекер импульса цен по символам.

    Идея: на каждой итерации мониторинга формируем «композитную» цену символа
    (например, медиану цен с разных бирж), накапливаем историю и считаем
    краткосрочные изменения (1-3 итерации назад), чтобы подсветить резкий рост.

    Не требует изменений в калькуляторе: достаточно передавать цены тикеров,
    которые уже собираются в main.py.
    """

    def __init__(
        self,
        *,
        window_sizes: Tuple[int, int] = (1, 3),
        max_history: int = 60,
        use_median: bool = True,
        spike_threshold_pct: float = 3.0,
    ) -> None:
        """
        :param window_sizes: Сдвиги (в итерациях) для сравнения, например (1, 3)
        :param max_history: Максимальная длина истории на символ
        :param use_median: Использовать медиану цен по биржам, иначе среднее
        :param spike_threshold_pct: Порог резкого роста (в %), чтобы пометить spike
        """
        self.window_sizes = window_sizes
        self.max_history = max_history
        self.use_median = use_median
        self.spike_threshold_pct = spike_threshold_pct

        # Храним историю как (timestamp, price), чтобы считать окна по времени
        self._history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=self.max_history)
        )
        # Последний снимок цен по биржам: symbol -> [prices across exchanges]
        self._last_snapshot: Dict[str, List[float]] = {}

    def reset(self) -> None:
        self._history.clear()

    def _composite(self, prices: Iterable[float]) -> Optional[float]:
        arr = [p for p in prices if p is not None and p > 0]
        if not arr:
            return None
        return median(arr) if self.use_median else (sum(arr) / len(arr))

    def update_from_tickers(
        self, tickers_by_exchange: Dict[str, Dict[str, float]]
    ) -> Dict[str, float]:
        """
        Принимает словарь {exchange: {symbol: price, ...}, ...},
        формирует композитную цену на символ и обновляет историю.

        :return: dict {symbol: composite_price} для символов, у которых есть цена
        """
    # Соберём цены по символам со всех бирж
        by_symbol: Dict[str, List[float]] = defaultdict(list)
        for _ex, sym_map in (tickers_by_exchange or {}).items():
            if not isinstance(sym_map, dict):
                continue
            for sym, price in sym_map.items():
                try:
                    p = float(price)
                    if p > 0:
                        by_symbol[sym].append(p)
                except Exception:
                    # игнорируем некорректные цены
                    continue

        # Обновим последний снимок по биржам
        self._last_snapshot = {sym: prices.copy() for sym, prices in by_symbol.items() if prices}

        composite_prices: Dict[str, float] = {}
        now = time.time()
        for sym, prices in by_symbol.items():
            comp = self._composite(prices)
            if comp is not None:
                self._history[sym].append((now, comp))
                composite_prices[sym] = comp

        return composite_prices

    def update_from_symbol_prices(self, prices: Dict[str, float], *, ts: Optional[float] = None) -> None:
        """Обновить историю напрямую из {symbol: price}.
        :param ts: Явная отметка времени (epoch seconds); если не указана — текущая.
        """
        now = time.time() if ts is None else float(ts)
        for sym, price in (prices or {}).items():
            try:
                p = float(price)
                if p > 0:
                    self._history[sym].append((now, p))
            except Exception:
                continue

    def _pct_change(self, current: float, prev: Optional[float]) -> Optional[float]:
        if prev is None or prev <= 0:
            return None
        try:
            return ((current - prev) / prev) * 100.0
        except ZeroDivisionError:
            return None

    def get_momentum(self, symbol: str) -> Dict[str, Optional[float]]:
        """
        Возвращает метрики импульса по символу:
        - change_1: % изменение к прошлой итерации
        - change_3: % изменение к цене 3 итерации назад (если доступно)
        - momentum_pct: наибольшее из положительных изменений для окон из window_sizes
        - spike: True, если momentum_pct >= spike_threshold_pct
        """
        dq = self._history.get(symbol)
        if not dq or len(dq) < 2:
            return {
                "change_1": None,
                "change_3": None,
                "momentum_pct": None,
                "spike": False,
            }

        # Извлекаем цены по последним точкам (итерации), не по времени
        current = dq[-1][1]
        results: Dict[str, Optional[float]] = {"change_1": None, "change_3": None}

        best_positive = 0.0
        for w in self.window_sizes:
            if len(dq) > w:
                prev = dq[-1 - w][1]
                ch = self._pct_change(current, prev)
                key = f"change_{w}"
                results[key] = ch
                if ch is not None and ch > best_positive:
                    best_positive = ch

        momentum = best_positive if best_positive > 0 else (results.get("change_1") or 0.0)
        return {
            **results,
            "momentum_pct": momentum,
            "spike": bool(momentum is not None and momentum >= self.spike_threshold_pct),
        }

    def get_window_metrics(self, symbol: str, window_sec: int = 300) -> Dict[str, Optional[float]]:
        """
        Метрики по временному окну (по умолчанию 5 минут):
        - momentum_5m_pct (change_pct): изменение текущей цены к цене на начале окна
        - spike_up_5m: True, если рост >= порога spike_threshold_pct
        - spike_down_5m: True, если падение >= порога spike_threshold_pct (по модулю)
        - volatility_5m_pct: (max - min) / min * 100 внутри окна
        - sample_count_5m: количество точек в окне
        """
        dq = self._history.get(symbol)
        if not dq or len(dq) < 2:
            return {
                "momentum_5m_pct": None,
                "spike_up_5m": False,
                "spike_down_5m": False,
                "volatility_5m_pct": None,
                "sample_count_5m": 0,
            }

        # Используем последний таймштамп в истории как «текущее время»,
        # чтобы корректно работать с историческими данными и тестами
        now = dq[-1][0]
        cutoff = now - float(window_sec)

        # Отбираем точки в окне времени
        window_points = [p for (ts, p) in dq if ts >= cutoff]

        if len(window_points) < 2:
            # Недостаточно данных за окно — используем доступные
            # но меняем только sample_count
            return {
                "momentum_5m_pct": None,
                "spike_up_5m": False,
                "spike_down_5m": False,
                "volatility_5m_pct": None,
                "sample_count_5m": len(window_points),
            }

        current = window_points[-1]
        start_price = window_points[0]
        change_pct = self._pct_change(current, start_price)

        v_min = min(window_points)
        v_max = max(window_points)
        volatility = None
        if v_min and v_min > 0:
            volatility = ((v_max - v_min) / v_min) * 100.0

        spike_up = bool(change_pct is not None and change_pct >= self.spike_threshold_pct)
        spike_down = bool(change_pct is not None and change_pct <= -self.spike_threshold_pct)

        return {
            "momentum_5m_pct": change_pct,
            "spike_up_5m": spike_up,
            "spike_down_5m": spike_down,
            "volatility_5m_pct": volatility,
            "sample_count_5m": len(window_points),
        }

    # Универсальные функции для любого окна
    def get_window_change_pct(self, symbol: str, window_sec: int) -> Optional[float]:
        dq = self._history.get(symbol)
        if not dq or len(dq) < 2:
            return None
        now = dq[-1][0]
        cutoff = now - float(window_sec)
        points = [p for (ts, p) in dq if ts >= cutoff]
        if len(points) < 2:
            return None
        return self._pct_change(points[-1], points[0])

    def get_window_volatility_pct(self, symbol: str, window_sec: int) -> Optional[float]:
        dq = self._history.get(symbol)
        if not dq or len(dq) < 2:
            return None
        now = dq[-1][0]
        cutoff = now - float(window_sec)
        points = [p for (ts, p) in dq if ts >= cutoff]
        if len(points) < 2:
            return None
        v_min = min(points)
        v_max = max(points)
        if v_min <= 0:
            return None
        return ((v_max - v_min) / v_min) * 100.0

    # --- Доп. метрики ---
    def get_slope_pct_per_min(self, symbol: str, window_sec: int = 300) -> Optional[float]:
        """Наклон тренда за окно в %/мин относительно стартовой цены.
        Простой OLS: y ~ a + b*t, t в минутах от начала окна.
        Возвращаем b/start_price*100.
        """
        dq = self._history.get(symbol)
        if not dq or len(dq) < 2:
            return None
        now = dq[-1][0]
        cutoff = now - float(window_sec)
        pts = [(ts, p) for (ts, p) in dq if ts >= cutoff]
        if len(pts) < 2:
            return None
        t0 = pts[0][0]
        start_price = pts[0][1]
        if start_price <= 0:
            return None
        xs: List[float] = [ (ts - t0)/60.0 for (ts, _p) in pts ]  # минуты
        ys: List[float] = [ p for (_ts, p) in pts ]
        n = float(len(xs))
        sx = sum(xs)
        sy = sum(ys)
        sxx = sum(x*x for x in xs)
        sxy = sum(x*y for x, y in zip(xs, ys))
        denom = (n * sxx - sx * sx)
        if denom == 0:
            return None
        b = (n * sxy - sx * sy) / denom
        slope_pct_per_min = (b / start_price) * 100.0
        return slope_pct_per_min

    def get_dispersion_current(self, symbol: str) -> Dict[str, Optional[float]]:
        """Текущая кросс-биржевая дисперсия цен: (max-min)/min*100 и число бирж с ценой."""
        arr = self._last_snapshot.get(symbol) or []
        arr = [float(x) for x in arr if x is not None and x > 0]
        if len(arr) < 2:
            return {"dispersion_pct": None, "exchange_count": len(arr)}
        v_min = min(arr)
        v_max = max(arr)
        if v_min <= 0:
            return {"dispersion_pct": None, "exchange_count": len(arr)}
        disp = ((v_max - v_min) / v_min) * 100.0
        return {"dispersion_pct": disp, "exchange_count": len(arr)}

    def get_heat_score(self, symbol: str, window_sec: int = 300) -> Optional[float]:
        """Простой интегральный скор 0..100: импульс, дисперсия и SNR, со штрафами за малое число точек/бирж."""
        w = self.get_window_metrics(symbol, window_sec=window_sec)
        mom = w.get("momentum_5m_pct") if w else None
        vol = w.get("volatility_5m_pct") if w else None
        samples = int(w.get("sample_count_5m") or 0) if w else 0
        d = self.get_dispersion_current(symbol)
        disp = d.get("dispersion_pct") if d else None
        exch = int(d.get("exchange_count") or 0) if d else 0

        if mom is None and disp is None:
            return None

        # Нормировки (грубые, но стабильные)
        abs_mom = abs(float(mom or 0.0))
        norm_mom = min(abs_mom / 5.0, 1.0)  # 5% -> 1.0
        norm_disp = min(float(disp or 0.0) / 2.0, 1.0)  # 2% -> 1.0
        if vol is None or vol <= 0:
            snr = 0.0
        else:
            snr = min((abs_mom / float(vol)), 2.0) / 2.0  # [0..1]

        penalty = 0.0
        if exch < 2:
            penalty += 0.5
        if samples < 3:
            penalty += 0.3
        penalty = min(penalty, 1.0)

        # Веса
        heat = 0.4 * norm_mom + 0.3 * norm_disp + 0.3 * snr - 0.3 * penalty
        heat = max(0.0, min(1.0, heat))
        return round(heat * 100.0, 1)
