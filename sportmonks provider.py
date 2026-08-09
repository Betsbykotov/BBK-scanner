"""
Sportmonks Provider — live xG и Pressure Index по матчам.

Работает синхронно (requests), как и остальные части сканера — никакого
asyncio, вписывается в существующий цикл run_cycle() / time.sleep().
Полностью изолирован от OddsCorp/Odds-API: если Sportmonks недоступен —
это не мешает основному циклу коэффициентов.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger("sportmonks_provider")

BASE_URL = "https://api.sportmonks.com/v3/football"

_DEBUG_DUMP_RAW_FIXTURE = False
_debug_dumped_once = False

# Сколько последних минут матча учитываем для "текущего" Pressure Index —
# отражает, что происходит прямо сейчас, а не с 1-й минуты матча.
PRESSURE_RECENT_WINDOW_MINUTES = 15


class SportmonksProvider:
    """Провайдер live-статистики (xG, Pressure Index) через Sportmonks API."""

    def __init__(self, api_token: str, timeout_seconds: int = 15):
        if not api_token:
            raise ValueError("SPORTMONKS_API_KEY не задан")
        self.api_token = api_token.strip()
        self.timeout_seconds = timeout_seconds

    def _request(self, path: str, params: dict | None = None) -> dict | None:
        params = dict(params or {})
        params["api_token"] = self.api_token
        url = f"{BASE_URL}{path}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            logger.warning(f"Sportmonks request error: {exc}")
            return None

        if resp.status_code != 200:
            logger.warning(f"Sportmonks HTTP {resp.status_code}: {resp.text[:300]}")
            return None

        try:
            return resp.json()
        except ValueError:
            logger.warning("Sportmonks: не удалось распарсить JSON ответа")
            return None

    def get_live_pressure_data(self) -> list[dict]:
        """
        Возвращает список live-матчей с нормализованными данными:
        [{fixture_id, home_team, away_team, minute, league_name,
          home_xg, away_xg, home_pressure, away_pressure, fetched_at, raw}, ...]

        Пустой список — если live-матчей нет или запрос не удался
        (никогда не бросает исключение наружу).
        """
        data = self._request(
            "/livescores",
            params={"include": "participants;scores;xgfixture;pressure;statistics.type;league"},
        )
        if not data or "data" not in data:
            return []

        global _debug_dumped_once
        if _DEBUG_DUMP_RAW_FIXTURE and not _debug_dumped_once:
            top_level_keys = list(data.keys())
            print(f"[SPORTMONKS DEBUG] top-level keys ответа: {top_level_keys}", flush=True)
            if "message" in data:
                print(f"[SPORTMONKS DEBUG] message: {data.get('message')}", flush=True)
            if "warnings" in data:
                print(f"[SPORTMONKS DEBUG] warnings: {data.get('warnings')}", flush=True)

            if data["data"]:
                raw_fixture = data["data"][0]
                dump = json.dumps(raw_fixture, ensure_ascii=False)
                chunk_size = 1500
                chunks = [dump[i:i + chunk_size] for i in range(0, len(dump), chunk_size)]
                print(f"[SPORTMONKS DEBUG] raw fixture, {len(chunks)} частей, длина {len(dump)}:", flush=True)
                for idx, chunk in enumerate(chunks):
                    print(f"[SPORTMONKS DEBUG] part {idx+1}/{len(chunks)}: {chunk}", flush=True)
                _debug_dumped_once = True

        results = []
        for fixture in data["data"]:
            try:
                normalized = self._normalize_fixture(fixture)
                if normalized:
                    results.append(normalized)
            except Exception as exc:
                fid = fixture.get("id", "unknown")
                logger.warning(f"Sportmonks: не смог распарсить fixture {fid}: {exc}")
                continue

        return results

    def _normalize_fixture(self, fixture: dict) -> dict | None:
        fixture_id = fixture.get("id")
        if fixture_id is None:
            return None

        participants = fixture.get("participants", []) or []
        home_name, away_name = "Home", "Away"
        home_id, away_id = None, None
        for p in participants:
            meta = p.get("meta", {}) or {}
            location = meta.get("location")
            if location == "home":
                home_name = p.get("name", "Home")
                home_id = p.get("id")
            elif location == "away":
                away_name = p.get("name", "Away")
                away_id = p.get("id")

        # Название лиги — приходит вместе с fixture, когда "league" запрошен
        # в include (см. get_live_pressure_data). Используется дальше в
        # notifier.py / pressure_detector.py для определения страны+флага
        # по тому же словарю-маппингу, что и SHARP/MOMENTUM алерты.
        league_block = fixture.get("league") or {}
        league_name = league_block.get("name")

        pressure_entries = fixture.get("pressure", []) or []
        xg_entries = fixture.get("xgfixture", []) or []

        time_block = fixture.get("time")
        minute = time_block.get("minute") if isinstance(time_block, dict) else fixture.get("minute")
        if not minute:
            minute = self._max_minute(pressure_entries) or self._max_minute(xg_entries)

        recent_pressure_entries = self._filter_by_minute_window(
            pressure_entries, max(0, minute - PRESSURE_RECENT_WINDOW_MINUTES)
        )
        home_pressure, away_pressure = self._sum_team_metric(
            recent_pressure_entries, home_id, away_id, value_keys=("pressure", "value")
        )
        home_pressure_total, away_pressure_total = self._sum_team_metric(
            pressure_entries, home_id, away_id, value_keys=("pressure", "value")
        )
        home_xg, away_xg = self._sum_team_metric(
            xg_entries, home_id, away_id, value_keys=("expected_goals", "xg", "value")
        )

        stats = fixture.get("statistics", []) or []
        home_shots, away_shots = self._extract_stat_by_name(stats, home_id, away_id, ("shots total", "total shots"))
        home_shots_on_target, away_shots_on_target = self._extract_stat_by_name(
            stats, home_id, away_id, ("shots on target", "shots on goal", "on target")
        )
        home_corners, away_corners = self._extract_stat_by_name(stats, home_id, away_id, ("corners",))
        home_possession, away_possession = self._extract_stat_by_name(stats, home_id, away_id, ("possession", "ball possession"))

        score_entries = fixture.get("scores", []) or []
        home_score, away_score = self._extract_current_score(score_entries, home_id, away_id)

        return {
            "fixture_id": fixture_id,
            "home_team": home_name,
            "away_team": away_name,
            "league_name": league_name,
            "minute": minute or 0,
            "home_score": home_score,
            "away_score": away_score,
            "home_xg": home_xg,
            "away_xg": away_xg,
            "home_pressure": home_pressure,
            "away_pressure": away_pressure,
            "home_pressure_total": home_pressure_total,
            "away_pressure_total": away_pressure_total,
            "home_shots": home_shots,
            "away_shots": away_shots,
            "home_shots_on_target": home_shots_on_target,
            "away_shots_on_target": away_shots_on_target,
            "home_corners": home_corners,
            "away_corners": away_corners,
            "home_possession": home_possession,
            "away_possession": away_possession,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "raw": fixture,
        }

    @staticmethod
    def _extract_current_score(entries: list, home_id, away_id) -> tuple[int, int]:
        """
        Достаёт текущий счёт матча из fixture['scores'].
        Предпочитаем запись с description == "CURRENT", фолбэк — максимум
        goals среди всех записей команды.
        """
        home_goals, away_goals = 0, 0
        home_current, away_current = None, None
        for entry in entries:
            participant_id = entry.get("participant_id")
            if participant_id not in (home_id, away_id):
                continue
            score_block = entry.get("score") or {}
            goals = score_block.get("goals")
            if goals is None:
                continue
            try:
                goals = int(goals)
            except (TypeError, ValueError):
                continue

            description = (entry.get("description") or "").upper()
            if participant_id == home_id:
                home_goals = max(home_goals, goals)
                if description == "CURRENT":
                    home_current = goals
            elif participant_id == away_id:
                away_goals = max(away_goals, goals)
                if description == "CURRENT":
                    away_current = goals

        home_result = home_current if home_current is not None else home_goals
        away_result = away_current if away_current is not None else away_goals
        return home_result, away_result

    @staticmethod
    def _max_minute(entries: list) -> int:
        """Максимальная 'minute' среди записей таймсерии (pressure/xgfixture)."""
        best = 0
        for entry in entries:
            m = entry.get("minute")
            try:
                m = int(m)
            except (TypeError, ValueError):
                continue
            if m > best:
                best = m
        return best

    @staticmethod
    def _filter_by_minute_window(entries: list, min_minute: int) -> list:
        """Оставляет только записи таймсерии с minute >= min_minute."""
        filtered = []
        for entry in entries:
            m = entry.get("minute")
            try:
                m = int(m)
            except (TypeError, ValueError):
                continue
            if m >= min_minute:
                filtered.append(entry)
        return filtered

    @staticmethod
    def _sum_team_metric(entries: list, home_id, away_id, value_keys: tuple[str, ...]) -> tuple[float, float]:
        """
        Суммирует значение метрики по home/away за все записи таймсерии.
        """
        home_val, away_val = 0.0, 0.0
        for entry in entries:
            participant_id = (
                entry.get("participant_id")
                or entry.get("team_id")
                or (entry.get("participant") or {}).get("id")
            )
            if participant_id not in (home_id, away_id):
                continue

            value = None
            data_block = entry.get("data")
            if isinstance(data_block, dict) and data_block.get("value") is not None:
                value = data_block.get("value")
            else:
                for key in value_keys:
                    if entry.get(key) is not None:
                        value = entry.get(key)
                        break

            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue

            if participant_id == home_id:
                home_val += value
            elif participant_id == away_id:
                away_val += value

        return round(home_val, 2), round(away_val, 2)

    @staticmethod
    def _extract_stat_by_name(entries: list, home_id, away_id, name_keywords: tuple[str, ...]) -> tuple[float, float]:
        """
        Достаёт значение статистики (удары, угловые, владение и т.п.) по названию типа.
        """
        home_val, away_val = 0.0, 0.0
        for entry in entries:
            type_block = entry.get("type") or {}
            type_name = (type_block.get("name") or entry.get("type_name") or "").lower()
            if not type_name or not any(kw in type_name for kw in name_keywords):
                continue

            participant_id = (
                entry.get("participant_id")
                or entry.get("team_id")
                or (entry.get("participant") or {}).get("id")
            )
            data_block = entry.get("data")
            value = data_block.get("value") if isinstance(data_block, dict) else entry.get("value")
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue

            if participant_id == home_id:
                home_val = value
            elif participant_id == away_id:
                away_val = value

        return home_val, away_val
