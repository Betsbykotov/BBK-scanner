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

CREATE TABLE IF NOT EXISTS pressure_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id TEXT NOT NULL,
    league_name TEXT,
    country TEXT,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    minute INTEGER,
    xg_home REAL,
    xg_away REAL,
    pressure_index_home REAL,
    pressure_index_away REAL,
    shots_home INTEGER,
    shots_away INTEGER,
    corners_home INTEGER,
    corners_away INTEGER,
    possession_home REAL,
    possession_away REAL,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pressure_lookup
ON pressure_snapshots(fixture_id, captured_at);
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

    def insert_pressure_snapshot(self, match: dict, captured_at: str) -> None:
        """Сохраняет один снимок давления/xG по матчу. match — сырой dict
        из sportmonks_provider.get_live_pressure_data(). Поля читаются через
        .get() с дефолтом None, чтобы отсутствие какого-то одного показателя
        не ломало запись остальных.

        ВАЖНО: имена полей (xg_home, pressure_index_home, и т.д.) — это
        предположение по смыслу. Нужно свериться с реальными ключами, которые
        отдаёт sportmonks_provider.get_live_pressure_data() и которые читает
        pressure_detector.detect_pressure_alerts() — если там другие названия,
        поправим на реальные перед первым запуском.
        """
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pressure_snapshots (
                    fixture_id, league_name, country, home_team, away_team, minute,
                    xg_home, xg_away, pressure_index_home, pressure_index_away,
                    shots_home, shots_away, corners_home, corners_away,
                    possession_home, possession_away, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(match.get("fixture_id") or match.get("id") or ""),
                    match.get("league_name") or match.get("league"),
                    match.get("country") or match.get("country_name") or match.get("league_country"),
                    match.get("home_team", ""),
                    match.get("away_team", ""),
                    match.get("minute"),
                    match.get("xg_home"),
                    match.get("xg_away"),
                    match.get("pressure_index_home"),
                    match.get("pressure_index_away"),
                    match.get("shots_home"),
                    match.get("shots_away"),
                    match.get("corners_home"),
                    match.get("corners_away"),
                    match.get("possession_home"),
                    match.get("possession_away"),
                    captured_at,
                ),
            )

    def get_pressure_history(self, fixture_id: str, window_minutes: int = 20) -> list[dict]:
        """Возвращает снимки за последние window_minutes для одного матча,
        отсортированные по времени по возрастанию — то, что рисует линию
        нарастания давления в карточке.
        """
        cutoff = (
            datetime.utcnow() - timedelta(minutes=window_minutes)
        ).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM pressure_snapshots
                WHERE fixture_id = ? AND captured_at >= ?
                ORDER BY captured_at ASC
                """,
                (fixture_id, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]
