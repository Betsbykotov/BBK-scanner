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
    country_name TEXT,
    country_iso2 TEXT,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    minute INTEGER,
    home_score INTEGER,
    away_score INTEGER,
    home_xg REAL,
    away_xg REAL,
    home_pressure REAL,
    away_pressure REAL,
    home_pressure_total REAL,
    away_pressure_total REAL,
    home_shots REAL,
    away_shots REAL,
    home_shots_on_target REAL,
    away_shots_on_target REAL,
    home_corners REAL,
    away_corners REAL,
    home_possession REAL,
    away_possession REAL,
    alert_score REAL,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pressure_lookup
ON pressure_snapshots(fixture_id, captured_at);
"""

# --- ФИКС (17.08.2026, зависание сканера на ~3 часа без единой строки в
# логах и без traceback) ---
# Диагноз: OddsDatabase.connect() открывал sqlite3.connect(self.path) БЕЗ
# параметра timeout — по умолчанию это 5 секунд, после чего sqlite3 кидает
# sqlite3.OperationalError("database is locked"). Это исключение НЕ входит
# в except (ProviderError, RuntimeError, ValueError) в главном цикле
# main.py, поэтому либо крашило процесс без явного рестарта Railway, либо
# (что вероятнее при частой конкурентной записи из run_cycle/
# run_pressure_cycle/run_pressure_cycle_v3, каждый из которых открывает
# СВОЁ соединение на запись почти одновременно) блокировка на уровне
# файловой системы контейнера держалась куда дольше 5 сек, создавая
# ощущение полного зависания.
#
# Решение:
# 1. DB_TIMEOUT_SECONDS — теперь connect() ждёт снятия блокировки явно
#    заданное время вместо дефолтных 5 сек, и это настраиваемо.
# 2. WAL (Write-Ahead Logging) — стандартный режим SQLite для конкурентного
#    доступа: читатели не блокируют писателя и наоборот, что резко снижает
#    частоту "database is locked" при нескольких открытых соединениях
#    (odds_snapshots / sent_alerts / pressure_snapshots пишутся из разных
#    функций в рамках одного цикла).
# 3. busy_timeout PRAGMA — дублирует Python-таймаут на уровне самого
#    SQLite-движка, страхует от блокировок внутри одного connect().
DB_TIMEOUT_SECONDS = 15.0


class OddsDatabase:
    def __init__(self, path: str, timeout_seconds: float = DB_TIMEOUT_SECONDS):
        self.path = path
        self.timeout_seconds = timeout_seconds
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
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

    def insert_pressure_snapshot(self, match: dict, captured_at: str, alert_score: float | None = None) -> None:
        """Сохраняет один снимок давления/xG по матчу. match — dict из
        sportmonks_provider.SportmonksProvider.get_live_pressure_data()
        (см. _normalize_fixture), поля соответствуют реальным ключам,
        которые также читает pressure_detector.detect_pressure_alerts().

        alert_score передаётся только когда на этом снимке сработал алерт
        (score из detect_pressure_alerts) — для остальных снимков None,
        что позволяет на графике карточки отметить точку, где был алерт.
        """
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pressure_snapshots (
                    fixture_id, league_name, country_name, country_iso2,
                    home_team, away_team, minute, home_score, away_score,
                    home_xg, away_xg, home_pressure, away_pressure,
                    home_pressure_total, away_pressure_total,
                    home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                    home_corners, away_corners, home_possession, away_possession,
                    alert_score, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(match.get("fixture_id") or ""),
                    match.get("league_name"),
                    match.get("country_name"),
                    match.get("country_iso2"),
                    match.get("home_team", ""),
                    match.get("away_team", ""),
                    match.get("minute"),
                    match.get("home_score"),
                    match.get("away_score"),
                    match.get("home_xg"),
                    match.get("away_xg"),
                    match.get("home_pressure"),
                    match.get("away_pressure"),
                    match.get("home_pressure_total"),
                    match.get("away_pressure_total"),
                    match.get("home_shots"),
                    match.get("away_shots"),
                    match.get("home_shots_on_target"),
                    match.get("away_shots_on_target"),
                    match.get("home_corners"),
                    match.get("away_corners"),
                    match.get("home_possession"),
                    match.get("away_possession"),
                    alert_score,
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
                (str(fixture_id), cutoff),
            ).fetchall()
        return [dict(row) for row in rows]
