from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

from models import OddsQuote


SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    sport_key TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    bookmaker_key TEXT NOT NULL,
    bookmaker_title TEXT NOT NULL,
    market_key TEXT NOT NULL,
    outcome_name TEXT NOT NULL,
    point REAL,
    price REAL NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_odds_lookup
ON odds_snapshots(event_id, bookmaker_key, market_key, outcome_name, point, captured_at);

CREATE TABLE IF NOT EXISTS sent_alerts (
    dedup_key TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);
"""


class OddsDatabase:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def insert_quotes(self, quotes: list[OddsQuote]) -> None:
        rows = [
            (
                q.event_id, q.sport_key, q.commence_time, q.home_team, q.away_team,
                q.bookmaker_key, q.bookmaker_title, q.market_key, q.outcome_name,
                q.point, q.price, q.captured_at,
            )
            for q in quotes
        ]
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO odds_snapshots (
                    event_id, sport_key, commence_time, home_team, away_team,
                    bookmaker_key, bookmaker_title, market_key, outcome_name,
                    point, price, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def previous_price(self, quote: OddsQuote, now_iso: str, lookback_start_iso: str) -> float | None:
        result = self.previous_quote(quote, now_iso, lookback_start_iso)
        return result[0] if result else None

    def previous_quote(
        self, quote: OddsQuote, now_iso: str, lookback_start_iso: str
    ) -> tuple[float, str] | None:
        """Возвращает (цена, captured_at) последнего снимка ДО now_iso,
        но не старше lookback_start_iso (окно поиска в прошлое).

        ВАЖНО: раньше сюда передавался только один параметр — начало окна
        (lookback_start), который в SQL использовался как ВЕРХНЯЯ граница
        (captured_at <= before_iso). Это был баг: запрос искал снимок СТАРШЕ
        начала окна поиска, а не "последний снимок В пределах окна". Пока
        сканер не проработал непрерывно дольше lookback_minutes, такая строка
        физически не могла существовать — отсюда movement/velocity всегда None.

        Теперь два явных параметра: верхняя граница (текущий момент) и нижняя
        (начало окна) — берём самый свежий снимок строго внутри этого окна.
        """
        point_condition = "point IS NULL" if quote.point is None else "point = ?"
        params: list[object] = [
            quote.event_id, quote.bookmaker_key, quote.market_key, quote.outcome_name
        ]
        if quote.point is not None:
            params.append(quote.point)
        params.append(lookback_start_iso)
        params.append(now_iso)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT price, captured_at
                FROM odds_snapshots
                WHERE event_id = ?
                  AND bookmaker_key = ?
                  AND market_key = ?
                  AND outcome_name = ?
                  AND {point_condition}
                  AND captured_at >= ?
                  AND captured_at < ?
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return (float(row["price"]), str(row["captured_at"])) if row else None

    def was_recently_sent(self, dedup_key: str, cooldown_minutes: int, now_iso: str) -> bool:
        """Проверяет, отправлялся ли уже алерт с этим ключом внутри окна cooldown.

        Ключ = конкретная линия у конкретного букмекера (event+market+outcome+
        bookmaker). Так одно и то же движение не спамит каждый цикл сканирования,
        но если линия продолжает жить дальше cooldown — алерт снова разрешён.
        """
        cutoff = (
            datetime.fromisoformat(now_iso) - timedelta(minutes=cooldown_minutes)
        ).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT sent_at FROM sent_alerts WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
        return bool(row and str(row["sent_at"]) > cutoff)

    def mark_sent(self, dedup_key: str, sent_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sent_alerts (dedup_key, sent_at) VALUES (?, ?)
                ON CONFLICT(dedup_key) DO UPDATE SET sent_at = excluded.sent_at
                """,
                (dedup_key, sent_at),
            )
