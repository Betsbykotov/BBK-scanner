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

    def previous_price(self, quote: OddsQ
