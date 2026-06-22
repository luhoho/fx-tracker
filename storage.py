"""
Зберігання історії курсів у SQLite.

Чому SQLite, а не CSV/JSON:
- Запити "середнє за 7 днів" / "мін/макс за 30 днів" — одним SQL.
- Безпечно дописувати з cron без локів і конкуренції.
- Файл переносний: хочеш бекап — скопіював один .db.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

DEFAULT_DB = Path(__file__).parent / "fx.db"


def _connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")  # кращий паралельний доступ
    return conn


def init_db(db_path: Path = DEFAULT_DB) -> None:
    """Створює таблицю, якщо її ще немає. Ідемпотентно."""
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rates (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      INTEGER NOT NULL,          -- unix timestamp
                source  TEXT    NOT NULL,          -- monobank | nbu | privatbank
                pair    TEXT    NOT NULL,          -- USD/UAH | EUR/UAH | EUR/USD
                buy     REAL,
                sell    REAL,
                cross   REAL
            );
            CREATE INDEX IF NOT EXISTS idx_rates_ts     ON rates(ts);
            CREATE INDEX IF NOT EXISTS idx_rates_lookup ON rates(source, pair, ts);

            CREATE TABLE IF NOT EXISTS alert_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        INTEGER NOT NULL,
                operation TEXT    NOT NULL,   -- sell_fx | buy_usd | buy_eur
                pair      TEXT    NOT NULL,
                score     INTEGER,
                rate      REAL,
                sent      INTEGER DEFAULT 1  -- 1=відправлено, 0=не відправлено (score < 60)
            );
            CREATE INDEX IF NOT EXISTS idx_alert_log_lookup
                ON alert_log(operation, pair, ts);
            """
        )


def save_rates(rates: Iterable[dict], db_path: Path = DEFAULT_DB) -> int:
    """Записує список курсів. Повертає кількість вставлених рядків."""
    rows = [
        (r["ts"], r["source"], r["pair"], r.get("buy"), r.get("sell"), r.get("cross"))
        for r in rates
    ]
    if not rows:
        return 0
    with _connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO rates (ts, source, pair, buy, sell, cross) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            rows,
        )
    return len(rows)


def latest(source: str, pair: str, db_path: Path = DEFAULT_DB) -> dict | None:
    """Останній запис для пари джерело/валюта."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM rates WHERE source = ? AND pair = ? "
            "ORDER BY ts DESC LIMIT 1;",
            (source, pair),
        ).fetchone()
    return dict(row) if row else None


def stats(
    source: str,
    pair: str,
    days: int,
    db_path: Path = DEFAULT_DB,
) -> dict | None:
    """
    Статистика за N днів: середнє/мін/макс/к-сть точок.
    Рахуємо по колонці 'buy' — тобто по тому курсу, за яким банк купує валюту
    (це курс, за яким ТИ продаєш долари в гривню). Для рішення про купівлю
    валюти назад використовуй 'sell' відповідно.
    """
    import time

    now = int(time.time())
    since = now - days * 86400
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)     AS n,
                AVG(buy)     AS avg_buy,
                MIN(buy)     AS min_buy,
                MAX(buy)     AS max_buy,
                AVG(sell)    AS avg_sell,
                MIN(sell)    AS min_sell,
                MAX(sell)    AS max_sell
            FROM rates
            WHERE source = ? AND pair = ? AND ts >= ?;
            """,
            (source, pair, since),
        ).fetchone()
    if not row or row["n"] == 0:
        return None
    return dict(row)


def history(
    source: str,
    pair: str,
    days: int,
    db_path: Path = DEFAULT_DB,
) -> list[dict]:
    """Повна історія за N днів — корисно для графіків або експорту."""
    import time

    since = int(time.time()) - days * 86400
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, buy, sell FROM rates "
            "WHERE source = ? AND pair = ? AND ts >= ? ORDER BY ts;",
            (source, pair, since),
        ).fetchall()
    return [dict(r) for r in rows]


def ma_history(
    source: str,
    pair: str,
    column: str,
    days: int = 14,
    db_path: Path = DEFAULT_DB,
) -> dict | None:
    """
    Повертає rolling MA та денний діапазон для score-розрахунку.

    column: "buy" або "sell"

    Повертає:
    {
        "ma":       float,   # середнє за days днів
        "current":  float,   # найостанніше значення
        "day_high": float,   # max за сьогодні (calendar day, Київ UTC+2)
        "day_low":  float,   # min за сьогодні
        "n":        int,     # кількість точок у вибірці
    }
    Повертає None якщо менше 3 точок.
    """
    import time

    if column not in ("buy", "sell"):
        raise ValueError(f"column має бути 'buy' або 'sell', отримано: {column!r}")

    now = int(time.time())
    since = now - days * 86400
    # Початок поточного дня Київ (UTC+2) — беремо з запасом UTC midnight
    kyiv_offset = 2 * 3600
    today_start = (now + kyiv_offset) // 86400 * 86400 - kyiv_offset

    with _connect(db_path) as conn:
        ma_row = conn.execute(
            f"""
            SELECT AVG({column}) AS ma, COUNT(*) AS n,
                   (SELECT {column} FROM rates
                    WHERE source=? AND pair=? AND {column} IS NOT NULL
                    ORDER BY ts DESC LIMIT 1) AS current
            FROM rates
            WHERE source=? AND pair=? AND ts >= ? AND {column} IS NOT NULL;
            """,
            (source, pair, source, pair, since),
        ).fetchone()

        day_row = conn.execute(
            f"""
            SELECT MAX({column}) AS day_high, MIN({column}) AS day_low
            FROM rates
            WHERE source=? AND pair=? AND ts >= ? AND {column} IS NOT NULL;
            """,
            (source, pair, today_start),
        ).fetchone()

    if not ma_row or ma_row["n"] < 3 or ma_row["current"] is None:
        return None

    return {
        "ma":       ma_row["ma"],
        "current":  ma_row["current"],
        "day_high": day_row["day_high"] if day_row and day_row["day_high"] is not None else ma_row["current"],
        "day_low":  day_row["day_low"]  if day_row and day_row["day_low"]  is not None else ma_row["current"],
        "n":        ma_row["n"],
    }


def log_alert(
    ts: int,
    operation: str,
    pair: str,
    score: int,
    rate: float,
    sent: int,
    db_path: Path = DEFAULT_DB,
) -> None:
    """Записує подію алерту в alert_log."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO alert_log (ts, operation, pair, score, rate, sent) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (ts, operation, pair, score, rate, sent),
        )


def last_alert_ts(
    operation: str,
    pair: str,
    db_path: Path = DEFAULT_DB,
) -> int | None:
    """
    Повертає unix ts останнього ВІДПРАВЛЕНОГО алерту (sent=1)
    для цієї операції+пари. None якщо ще не було.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(ts) AS last_ts FROM alert_log "
            "WHERE operation=? AND pair=? AND sent=1;",
            (operation, pair),
        ).fetchone()
    return row["last_ts"] if row and row["last_ts"] is not None else None


if __name__ == "__main__":
    init_db()
    print(f"DB готова: {DEFAULT_DB}")
