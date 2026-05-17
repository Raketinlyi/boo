import os
import sqlite3
import logging
import time
import traceback
from typing import List, Tuple, Dict, Optional, Any

class Database:
    """
    Класс для работы с базой данных SQLite.
    
    Отвечает за:
    - Инициализацию базы данных
    - Сохранение арбитражных возможност��й
    - Получение данных из базы данных
    """
    
    DB_SCHEMA = """
    CREATE TABLE IF NOT EXISTS arbitrage_opportunities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        buy_exchange TEXT NOT NULL,
        sell_exchange TEXT NOT NULL,
        buy_price REAL NOT NULL,
        sell_price REAL NOT NULL,
        spread REAL NOT NULL,
        timestamp TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_timestamp ON arbitrage_opportunities (timestamp);
    CREATE INDEX IF NOT EXISTS idx_symbol ON arbitrage_opportunities (symbol);

    -- CoinGecko persistent memory: current cache and history
    CREATE TABLE IF NOT EXISTS cg_markets_history (
        symbol TEXT NOT NULL,
        ts INTEGER NOT NULL,
        vol24 REAL,
        mcap REAL,
        source TEXT,
        PRIMARY KEY (symbol, ts)
    );
    CREATE INDEX IF NOT EXISTS idx_cg_hist_ts ON cg_markets_history(ts);
    CREATE INDEX IF NOT EXISTS idx_cg_hist_symbol ON cg_markets_history(symbol);

    CREATE TABLE IF NOT EXISTS cg_cache (
        symbol TEXT PRIMARY KEY,
        ts INTEGER,
        vol24 REAL,
        mcap REAL
    );

    CREATE TABLE IF NOT EXISTS exchange_asset_metadata_state (
        asset TEXT PRIMARY KEY,
        refreshed_at INTEGER NOT NULL,
        rows_count INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_exchange_asset_metadata_state_refresh
        ON exchange_asset_metadata_state(refreshed_at);

    CREATE TABLE IF NOT EXISTS exchange_asset_metadata (
        asset TEXT NOT NULL,
        exchange_name TEXT NOT NULL,
        chain TEXT NOT NULL,
        contract TEXT,
        deposit_enabled INTEGER,
        withdraw_enabled INTEGER,
        withdraw_fee TEXT,
        min_withdraw TEXT,
        refreshed_at INTEGER NOT NULL,
        PRIMARY KEY (asset, exchange_name, chain)
    );
    CREATE INDEX IF NOT EXISTS idx_exchange_asset_metadata_asset_refresh
        ON exchange_asset_metadata(asset, refreshed_at);
    CREATE INDEX IF NOT EXISTS idx_exchange_asset_metadata_refresh
        ON exchange_asset_metadata(refreshed_at);
    """
    INSERT_OPPORTUNITY_QUERY = """
    INSERT INTO arbitrage_opportunities
    (symbol, buy_exchange, sell_exchange, buy_price, sell_price, spread, timestamp)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """

    def __init__(self, db_path: str = "arbitrage.db"):
        """
        Инициализирует объект базы данных.
        
        Args:
            db_path: Путь к файлу базы данных
        """
        self.db_path = db_path
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """
        Получает соединение с базой данных.
        
        Returns:
            Объект соединения с базой данных
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def init_db(self):
        """Инициализирует базу данных, создавая необходимые таблицы и индексы."""
        try:
            # Создаем директорию, если она не существует
            directory = os.path.dirname(self.db_path)
            if directory and not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
                logging.info(f"Создана директория для базы данных: {directory}")
                
            with self._get_connection() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                cursor = conn.cursor()
                cursor.executescript(self.DB_SCHEMA)
                conn.commit()
            logging.info(f"База данных {self.db_path} инициализирована успешно.")
        except sqlite3.Error as e:
            logging.error(f"Ошибка инициализации базы данных SQLite: {e}\n{traceback.format_exc()}")
            raise

    # --- CoinGecko cache/history API ---
    def upsert_cg_cache(self, symbol: str, ts: int, vol24: Optional[float], mcap: Optional[float]) -> bool:
        """Upsert актуальные метрики CoinGecko для символа в таблицу кэша."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO cg_cache (symbol, ts, vol24, mcap)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET ts=excluded.ts, vol24=excluded.vol24, mcap=excluded.mcap
                    """,
                    (str(symbol).upper(), int(ts), vol24, mcap),
                )
                conn.commit()
            return True
        except Exception as e:
            logging.error(f"Error upserting cg_cache for {symbol}: {e}")
            return False

    def save_cg_market(
        self,
        symbol: str,
        ts: int,
        vol24: Optional[float],
        mcap: Optional[float],
        source: str = "batch",
    ) -> bool:
        """Сохраняет точку истории markets для символа (PRIMARY KEY(symbol, ts))."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO cg_markets_history (symbol, ts, vol24, mcap, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(symbol).upper(), int(ts), vol24, mcap, source),
                )
                conn.commit()
            return True
        except Exception as e:
            logging.error(f"Error saving cg_markets_history for {symbol}: {e}")
            return False

    def get_cg_recent_symbols(self, hours: int = 24, limit: int = 1000) -> List[Tuple[str, float]]:
        """Возвращает символы, появлявшиеся в истории за последние N часов, с их последним ts."""
        try:
            cutoff = int(time.time()) - int(hours * 3600)
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT symbol, MAX(ts) AS last_ts
                    FROM cg_markets_history
                    WHERE ts >= ?
                    GROUP BY symbol
                    ORDER BY last_ts DESC
                    LIMIT ?
                    """,
                    (cutoff, limit),
                )
                rows = cur.fetchall()
                return [(str(r[0]).upper(), float(r[1])) for r in rows]
        except Exception as e:
            logging.error(f"Error querying recent CG symbols: {e}")
            return []

    def prune_cg_history(self, retention_days: int = 7) -> int:
        """Удаляет записи истории старше retention_days. Возвращает число удалённых строк."""
        try:
            if retention_days is None or retention_days <= 0:
                return 0
            cutoff = int(time.time()) - int(retention_days * 86400)
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM cg_markets_history WHERE ts < ?", (cutoff,))
                deleted = cur.rowcount or 0
                conn.commit()
            return int(deleted)
        except Exception as e:
            logging.error(f"Error pruning CG history: {e}")
            return 0

    def prune_arbitrage_history(self, retention_days: int = 1) -> int:
        """Удаляет арбитражную историю старше retention_days. Возвращает число удалённых строк."""
        try:
            if retention_days is None or retention_days <= 0:
                return 0
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM arbitrage_opportunities WHERE timestamp < datetime('now', '-' || ? || ' days')",
                    (str(int(retention_days)),),
                )
                deleted = cur.rowcount or 0
                conn.commit()
            return int(deleted)
        except Exception as e:
            logging.error(f"Error pruning arbitrage history: {e}")
            return 0

    def get_cg_cache(self) -> Dict[str, Dict[str, Optional[float]]]:
        """Читает весь кэш CG в память как словарь symbol -> {ts, vol24, mcap}."""
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT symbol, ts, vol24, mcap FROM cg_cache")
                out: Dict[str, Dict[str, Optional[float]]] = {}
                for sym, ts, vol24, mcap in cur.fetchall():
                    out[str(sym).upper()] = {
                        "ts": float(ts) if ts is not None else None,
                        "vol24": vol24,
                        "mcap": mcap,
                    }
                return out
        except Exception as e:
            logging.error(f"Error reading cg_cache: {e}")
            return {}

    # --- Persistent exchange metadata cache ---
    def save_exchange_asset_metadata(self, asset: str, rows: List[Dict[str, Any]], refreshed_at: Optional[int] = None) -> bool:
        """Сохраняет снимок сетей/контрактов по активу для всех бирж."""
        asset_u = str(asset or "").strip().upper()
        if not asset_u:
            return False
        ts = int(refreshed_at or time.time())

        def _to_int_bool(value: Any) -> Optional[int]:
            if value is None:
                return None
            return 1 if bool(value) else 0

        deduped_rows: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            exchange_name = str(row.get("exchange") or row.get("exchange_name") or "").strip()
            chain = str(row.get("chain") or "-").strip() or "-"
            if not exchange_name:
                continue
            prepared_row = (
                asset_u,
                exchange_name,
                chain,
                str(row.get("contract") or row.get("contract_address") or "").strip() or None,
                _to_int_bool(row.get("deposit_enabled")),
                _to_int_bool(row.get("withdraw_enabled")),
                None if row.get("withdraw_fee") is None else str(row.get("withdraw_fee")),
                None if row.get("min_withdraw") is None else str(row.get("min_withdraw")),
                ts,
            )
            dedupe_key = (exchange_name, chain)
            current = deduped_rows.get(dedupe_key)
            if current is None:
                deduped_rows[dedupe_key] = prepared_row
                continue

            current_score = int(bool(current[3])) + int(current[4] is not None) + int(current[5] is not None)
            new_score = int(bool(prepared_row[3])) + int(prepared_row[4] is not None) + int(prepared_row[5] is not None)
            if new_score > current_score:
                deduped_rows[dedupe_key] = prepared_row

        prepared_rows = list(deduped_rows.values())

        try:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM exchange_asset_metadata WHERE asset = ?", (asset_u,))
                if prepared_rows:
                    conn.executemany(
                        """
                        INSERT INTO exchange_asset_metadata (
                            asset,
                            exchange_name,
                            chain,
                            contract,
                            deposit_enabled,
                            withdraw_enabled,
                            withdraw_fee,
                            min_withdraw,
                            refreshed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        prepared_rows,
                    )
                conn.execute(
                    """
                    INSERT INTO exchange_asset_metadata_state (asset, refreshed_at, rows_count)
                    VALUES (?, ?, ?)
                    ON CONFLICT(asset) DO UPDATE SET
                        refreshed_at = excluded.refreshed_at,
                        rows_count = excluded.rows_count
                    """,
                    (asset_u, ts, len(prepared_rows)),
                )
                conn.commit()
            return True
        except Exception as e:
            logging.error(f"Error saving exchange metadata for {asset_u}: {e}")
            return False

    def get_exchange_asset_metadata(self, asset: str, max_age_sec: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Читает сохранённый снимок сетей/контрактов по активу."""
        asset_u = str(asset or "").strip().upper()
        if not asset_u:
            return None
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT refreshed_at, rows_count FROM exchange_asset_metadata_state WHERE asset = ?",
                    (asset_u,),
                )
                state_row = cur.fetchone()
                if not state_row:
                    return None
                refreshed_at = int(state_row[0] or 0)
                rows_count = int(state_row[1] or 0)
                if max_age_sec is not None and max_age_sec >= 0:
                    if refreshed_at <= 0 or (time.time() - refreshed_at) > float(max_age_sec):
                        return None
                cur.execute(
                    """
                    SELECT exchange_name, chain, contract, deposit_enabled, withdraw_enabled, withdraw_fee, min_withdraw, refreshed_at
                    FROM exchange_asset_metadata
                    WHERE asset = ?
                    ORDER BY exchange_name, chain
                    """,
                    (asset_u,),
                )
                rows = []
                for exchange_name, chain, contract, deposit_enabled, withdraw_enabled, withdraw_fee, min_withdraw, row_ts in cur.fetchall():
                    rows.append({
                        "exchange": exchange_name,
                        "asset": asset_u,
                        "chain": chain,
                        "contract": contract,
                        "deposit_enabled": None if deposit_enabled is None else bool(deposit_enabled),
                        "withdraw_enabled": None if withdraw_enabled is None else bool(withdraw_enabled),
                        "withdraw_fee": withdraw_fee,
                        "min_withdraw": min_withdraw,
                        "refreshed_at": int(row_ts or refreshed_at),
                    })
                if not rows and rows_count == 0:
                    return {
                        "asset": asset_u,
                        "refreshed_at": refreshed_at,
                        "rows": [],
                    }
                return {
                    "asset": asset_u,
                    "refreshed_at": refreshed_at,
                    "rows": rows,
                }
        except Exception as e:
            logging.error(f"Error reading exchange metadata for {asset_u}: {e}")
            return None

    def is_exchange_asset_metadata_fresh(self, asset: str, max_age_sec: int) -> bool:
        """Быстрая проверка свежести сохранённого снимка по активу."""
        asset_u = str(asset or "").strip().upper()
        if not asset_u:
            return False
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT refreshed_at FROM exchange_asset_metadata_state WHERE asset = ?",
                    (asset_u,),
                )
                row = cur.fetchone()
                if not row:
                    return False
                refreshed_at = int(row[0] or 0)
                return refreshed_at > 0 and (time.time() - refreshed_at) <= float(max_age_sec)
        except Exception as e:
            logging.error(f"Error checking exchange metadata freshness for {asset_u}: {e}")
            return False

    def get_transfer_viability_bulk(self, asset_exchange_pairs: List[Tuple[str, str]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Bulk-запрос статуса депозита/вывода для списка (asset, exchange) пар.

        Возвращает словарь {(ASSET, exchange_name): {"deposit_enabled": bool|None, "withdraw_enabled": bool|None,
        "best_withdraw_fee": str|None, "chains_with_withdraw": list, "chains_with_deposit": list}}

        Использует один SQL-запрос для эффективности.
        """
        if not asset_exchange_pairs:
            return {}
        result: Dict[Tuple[str, str], Dict[str, Any]] = {}
        try:
            # Нормализуем
            normalized = [(str(a or "").strip().upper(), str(e or "").strip()) for a, e in asset_exchange_pairs]
            unique_assets = list({a for a, _ in normalized if a})
            if not unique_assets:
                return {}

            placeholders = ",".join("?" * len(unique_assets))
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""SELECT asset, exchange_name, chain, deposit_enabled, withdraw_enabled, withdraw_fee
                        FROM exchange_asset_metadata
                        WHERE asset IN ({placeholders})""",
                    unique_assets,
                )
                rows_raw = cur.fetchall()

            # Группируем: (asset, exchange) -> list of chain rows
            from collections import defaultdict
            grouped: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
            for asset_val, exchange_name, chain, dep_en, wd_en, wd_fee in rows_raw:
                grouped[(asset_val, exchange_name)].append({
                    "chain": chain,
                    "deposit_enabled": None if dep_en is None else bool(dep_en),
                    "withdraw_enabled": None if wd_en is None else bool(wd_en),
                    "withdraw_fee": wd_fee,
                })

            for asset_key, exchange_key in normalized:
                chain_rows = grouped.get((asset_key, exchange_key), [])
                chains_with_withdraw = [r["chain"] for r in chain_rows if r["withdraw_enabled"] is True]
                chains_with_deposit = [r["chain"] for r in chain_rows if r["deposit_enabled"] is True]
                # Выбираем лучшую комиссию вывода среди сетей где вывод включён
                best_fee = None
                for r in chain_rows:
                    if r["withdraw_enabled"] and r["withdraw_fee"] is not None:
                        try:
                            fee_val = float(r["withdraw_fee"])
                            if best_fee is None or fee_val < float(best_fee):
                                best_fee = r["withdraw_fee"]
                        except (ValueError, TypeError):
                            pass
                result[(asset_key, exchange_key)] = {
                    "deposit_enabled": any(r["deposit_enabled"] is True for r in chain_rows) if chain_rows else None,
                    "withdraw_enabled": any(r["withdraw_enabled"] is True for r in chain_rows) if chain_rows else None,
                    "best_withdraw_fee": best_fee,
                    "chains_with_withdraw": chains_with_withdraw,
                    "chains_with_deposit": chains_with_deposit,
                    "in_db": bool(chain_rows),
                }
        except Exception as e:
            logging.error(f"Error in get_transfer_viability_bulk: {e}")
        return result

    def merge_exchange_asset_metadata(self, asset: str, rows: List[Dict[str, Any]], refreshed_at: Optional[int] = None) -> bool:
        """Аккуратно объединяет новые строки метаданных по активу с уже сохранёнными."""
        asset_u = str(asset or "").strip().upper()
        if not asset_u:
            return False
        existing_payload = self.get_exchange_asset_metadata(asset_u, max_age_sec=None) or {"rows": []}
        existing_rows = existing_payload.get("rows") or []

        merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in existing_rows:
            if not isinstance(row, dict):
                continue
            exchange_name = str(row.get("exchange") or row.get("exchange_name") or "").strip()
            chain = str(row.get("chain") or "-").strip() or "-"
            if not exchange_name:
                continue
            merged[(exchange_name, chain)] = dict(row)

        for row in rows or []:
            if not isinstance(row, dict):
                continue
            exchange_name = str(row.get("exchange") or row.get("exchange_name") or "").strip()
            chain = str(row.get("chain") or "-").strip() or "-"
            if not exchange_name:
                continue
            key = (exchange_name, chain)
            current = dict(merged.get(key) or {})
            incoming = dict(row)
            merged[key] = {
                "exchange": exchange_name,
                "asset": asset_u,
                "chain": chain,
                "contract": incoming.get("contract") or incoming.get("contract_address") or current.get("contract") or current.get("contract_address"),
                "deposit_enabled": incoming.get("deposit_enabled") if incoming.get("deposit_enabled") is not None else current.get("deposit_enabled"),
                "withdraw_enabled": incoming.get("withdraw_enabled") if incoming.get("withdraw_enabled") is not None else current.get("withdraw_enabled"),
                "withdraw_fee": incoming.get("withdraw_fee") if incoming.get("withdraw_fee") is not None else current.get("withdraw_fee"),
                "min_withdraw": incoming.get("min_withdraw") if incoming.get("min_withdraw") is not None else current.get("min_withdraw"),
            }

        return self.save_exchange_asset_metadata(asset_u, list(merged.values()), refreshed_at=refreshed_at)

    def save_opportunity(self, opp: dict):
        """
        Сохраняет арбитражную возможность в базу данных.
        
        Args:
            opp: Словарь с информацией об арбитражной возможности
        """
        try:
            with self._get_connection() as conn:
                conn.execute(self.INSERT_OPPORTUNITY_QUERY, (
                    opp["symbol"], opp["buy_exchange"], opp["sell_exchange"],
                    opp["buy_price"], opp["sell_price"], opp["spread"], opp["timestamp"]
                ))
                conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Ошибка сохранения арбитражной возможности в БД: {e}\n{traceback.format_exc()}")
        except Exception as e:
             logging.error(f"Непредвиденная ошибка при сохранении в БД: {e}\n{traceback.format_exc()}")

    def save_opportunities_batch(self, opportunities: List[dict]) -> int:
        """Сохраняет список возможностей одним батчем (быстрее и меньше блокирует event loop)."""
        if not opportunities:
            return 0

        rows = []
        for opp in opportunities:
            try:
                rows.append((
                    opp["symbol"], opp["buy_exchange"], opp["sell_exchange"],
                    opp["buy_price"], opp["sell_price"], opp["spread"], opp["timestamp"]
                ))
            except Exception:
                continue

        if not rows:
            return 0

        try:
            with self._get_connection() as conn:
                conn.executemany(self.INSERT_OPPORTUNITY_QUERY, rows)
                conn.commit()
            return len(rows)
        except Exception as e:
            logging.error(f"Error saving opportunities batch: {e}\n{traceback.format_exc()}")
            return 0

    def get_opportunities_by_time(self, minutes: int) -> List[Tuple]:
        """
        Получает арбитражные возможности за указанный период времени.
        
        Args:
            minutes: Количество минут от текущего времени
            
        Returns:
            Список кортежей с информацией об арбитражных возможностях
        """
        query = """
            SELECT symbol, buy_exchange, sell_exchange, buy_price, sell_price, spread, timestamp
            FROM arbitrage_opportunities
            WHERE timestamp >= datetime('now', '-' || ? || ' minutes')
            ORDER BY timestamp DESC
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, (str(minutes),))
                return cursor.fetchall()
        except sqlite3.Error as e:
            logging.error(f"Ошибка получения арбитражных возможностей из БД: {e}\n{traceback.format_exc()}")
            return []
        except Exception as e:
             logging.error(f"Непредвиденная ошибка при получении из БД: {e}\n{traceback.format_exc()}")
             return []

    def get_opportunities_last_24h(self) -> List[Tuple]:
        """
        Получает арбитражные возможности за последние 24 часа.
        
        Returns:
            Список кортежей с информацией об арбитражных возможностях
        """
        return self.get_opportunities_by_time(24 * 60)
