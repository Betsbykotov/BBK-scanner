"""
Провайдер live-статистики (xG, удары, угловые, владение) с TheStatsAPI —
источник данных для v3.0 PRESSURE-ветки (параллельно Sportmonks/v2.0,
который остаётся нетронутым в sportmonks_provider.py).

У TheStatsAPI нет проприетарного "Pressure Index" (это метрика только
у Sportmonks) — поэтому home_pressure/away_pressure всегда возвращаются
нулевыми. pressure_detector-логика это штатно пропускает (see _share_gap_pct
-> None при total<=0), сигнал строится на xG + ударах в створ + угловых +
владении, этого достаточно (MIN_METRICS_REQUIRED = 2).

xG берём из /shotmap (np_xg_summary.live) — это живой, ещё не устоявшийся
xG на данный момент матча, что и нужно для live-детекции.
Остальное — из /live-stats.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from thestatsapi_rate_limiter import throttle as _shared_throttle

BASE_URL = "https://api.thestatsapi.com/api"


class ThestatsapiPressureProvider:
    MAX_MATCHES_PER_CYCLE = 20  # защита от переполнения цикла при большом кол-ве live-матчей

    def __init__(self, api_key: str, daily_budget: int = 3000):
        self.api_key = api_key
        self.daily_budget = daily_budget
        self._budget_used = 0
        self._budget_date = None
        self._competition_name_cache: dict[str, tuple[str, str]] = {}  # comp_id -> (name, country)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _check_budget(self) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        if self._budget_date != today:
            self._budget_date = today
            self._budget_used = 0
        if self._budget_used >= self.daily_budget:
            return False
        self._budget_used += 1
        return True

    def _get(self, path: str, params: dict | None = None, retry_on_429: bool = True) -> dict | None:
        if not self.api_key or not self._check_budget():
            return None
        _shared_throttle()
        try:
            resp = requests.get(f"{BASE_URL}{path}", headers=self._headers(), params=params or {}, timeout=10)
            if resp.status_code == 429 and retry_on_429:
                # Один повтор с паузой подольше — если лимит всё равно словили
                # несмотря на общий троттлер (например, всплеск от другого
                # потока в этот же момент), даём API отдышаться.
                time.sleep(1.5)
                resp = requests.get(f"{BASE_URL}{path}", headers=self._headers(), params=params or {}, timeout=10)
            if resp.status_code != 200:
                print(f"[thestatsapi-pressure] HTTP {resp.status_code} на {path}: {resp.text[:150]}", flush=True)
                return None
            return resp.json()
        except requests.RequestException as exc:
            print(f"[thestatsapi-pressure] сетевая ошибка на {path}: {exc}", flush=True)
            return None

    def _competition_info(self, competition_id: str) -> tuple[str, str]:
        """Возвращает (название лиги, страна). Кешируется — соревнования не
        меняются в течение прогона, незачем дёргать API каждый цикл."""
        if competition_id in self._competition_name_cache:
            return self._competition_name_cache[competition_id]
        data = self._get(f"/football/competitions/{competition_id}")
        name, country = "", ""
        if data and data.get("data"):
            name = data["data"].get("name", "")
            country = data["data"].get("country") or ""
        self._competition_name_cache[competition_id] = (name, country)
        return name, country

    def get_live_pressure_data(self) -> list[dict]:
        """Собирает live-матчи с xG/статистикой в формате, совместимом
        с SportmonksProvider.get_live_pressure_data(), чтобы его можно было
        скормить в тот же pressure_detector.detect_pressure_alerts()."""
        if not self.api_key:
            return []

        live_data = self._get("/football/matches", {"status": "live", "per_page": 50})
        if not live_data:
            return []

        matches_raw = live_data.get("data", [])
        matches_raw = matches_raw[: self.MAX_MATCHES_PER_CYCLE]
        results: list[dict] = []

        for m in matches_raw:
            match_id = m.get("id")
            if not match_id:
                continue

            live_stats_data = self._get(f"/football/matches/{match_id}/live-stats")
            if not live_stats_data:
                continue
            live = live_stats_data.get("data", {})
            meta = live.get("meta", {})
            stats = live.get("stats", {})

            minute = meta.get("elapsed_minutes") or 0

            def _stat(key: str) -> tuple[float, float]:
                block = stats.get(key, {}).get("all", {})
                return float(block.get("home", 0) or 0), float(block.get("away", 0) or 0)

            possession_h, possession_a = _stat("ball_possession")
            shots_on_target_h, shots_on_target_a = _stat("shots_on_target")
            shots_h, shots_a = _stat("total_shots")
            corners_h, corners_a = _stat("corner_kicks")

            # Живой xG — отдельный запрос к shotmap (live-stats его не даёт)
            xg_h, xg_a = 0.0, 0.0
            shotmap_data = self._get(f"/football/matches/{match_id}/shotmap")
            if shotmap_data:
                live_xg = shotmap_data.get("np_xg_summary", {}).get("live", {})
                xg_h = float(live_xg.get("home_team", 0) or 0)
                xg_a = float(live_xg.get("away_team", 0) or 0)

            competition_id = m.get("competition_id", "")
            league_name, country_name = self._competition_info(competition_id) if competition_id else ("", "")

            home_team = m.get("home_team", {}).get("name", "?")
            away_team = m.get("away_team", {}).get("name", "?")
            score = m.get("score", {}) or {}

            results.append({
                "fixture_id": str(match_id),
                "home_team": home_team,
                "away_team": away_team,
                "minute": int(minute),
                "home_score": score.get("home") or 0,
                "away_score": score.get("away") or 0,
                "home_xg": xg_h,
                "away_xg": xg_a,
                # У TheStatsAPI нет Pressure Index — оставляем нулями,
                # detect_pressure_alerts штатно пропустит эту метрику.
                "home_pressure": 0.0,
                "away_pressure": 0.0,
                "home_pressure_total": 0.0,
                "away_pressure_total": 0.0,
                "home_shots_on_target": shots_on_target_h,
                "away_shots_on_target": shots_on_target_a,
                "home_shots": shots_h,
                "away_shots": shots_a,
                "home_corners": corners_h,
                "away_corners": corners_a,
                "home_possession": possession_h,
                "away_possession": possession_a,
                "league_name": league_name,
                "country_name": country_name,
                "country_iso2": None,  # у TheStatsAPI нет ISO2 в competition-ответе
            })

        return results
