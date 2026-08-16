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

--- ИЗМЕНЕНИЯ (16.08.2026, фикс "молчания" v3.0) ---
1. Circuit breaker: если подряд ловим 429 несколько раз за цикл — это
   значит дневной/минутный лимит выжран целиком прямо сейчас (например,
   кто-то параллельно гонял thestatsapi_test.py и съел квоту вручную).
   Долбить оставшиеся матчи бессмысленно и только продлевает rate-limit —
   прерываем цикл раньше и возвращаем то, что успели собрать.
2. Экономия запросов: detector всё равно игнорирует матчи вне минут
   MIN_USEFUL_MINUTE..MAX_USEFUL_MINUTE — теперь не дёргаем дорогой
   /shotmap для матчей вне этого окна (получаем минуту из /live-stats,
   которая всё равно нужна, и только потом решаем, нужен ли shotmap).
3. Честные логи: если сам запрос списка live-матчей упал (429/сеть/
   пустой ответ) — теперь это явно залогировано как ошибка, а не тихо
   превращается в "живых матчей нет" (раньше _get возвращал None при
   ошибке и код молча делал return [] — на верхнем уровне это выглядело
   идентично случаю "матчей действительно нет").
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from thestatsapi_rate_limiter import throttle as _shared_throttle

BASE_URL = "https://api.thestatsapi.com/api"

# Совпадает с MIN_MINUTE/MAX_MINUTE в thestatsapi_pressure_detector.py.
# Продублировано намеренно (см. комментарий в детекторе про сознательное
# дублирование логики v3.0-ветки) — не тратим лишний запрос shotmap на
# матчи, которые detector всё равно отбросит по минуте.
MIN_USEFUL_MINUTE = 3
MAX_USEFUL_MINUTE = 80

# Если подряд столько раз словили 429 (даже после внутреннего ретрая) —
# считаем, что лимит выжран целиком на эту минуту и прекращаем цикл,
# а не долбим оставшиеся матчи в стену.
MAX_CONSECUTIVE_RATE_LIMITS = 3


def _log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] [thestatsapi-pressure] {message}", flush=True)


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

    def _get(self, path: str, params: dict | None = None, retry_on_429: bool = True) -> tuple[dict | None, bool]:
        """Возвращает (данные, был_ли_rate_limit).

        Второй элемент нужен, чтобы вызывающий код (get_live_pressure_data)
        мог отличить "429 несмотря на ретрай" от прочих ошибок и включить
        circuit breaker именно на rate limit, а не на любую сетевую икоту.
        """
        if not self.api_key or not self._check_budget():
            return None, False
        _shared_throttle()
        try:
            resp = requests.get(f"{BASE_URL}{path}", headers=self._headers(), params=params or {}, timeout=10)
            if resp.status_code == 429 and retry_on_429:
                # Один повтор с паузой подольше — если лимит всё равно словили
                # несмотря на общий троттлер (например, всплеск от другого
                # потока в этот же момент), даём API отдышаться.
                time.sleep(1.5)
                _shared_throttle()
                resp = requests.get(f"{BASE_URL}{path}", headers=self._headers(), params=params or {}, timeout=10)
            if resp.status_code == 429:
                _log(f"HTTP 429 на {path} (после ретрая) — лимит выжран")
                return None, True
            if resp.status_code != 200:
                _log(f"HTTP {resp.status_code} на {path}: {resp.text[:150]}")
                return None, False
            return resp.json(), False
        except requests.RequestException as exc:
            _log(f"сетевая ошибка на {path}: {exc}")
            return None, False

    def _competition_info(self, competition_id: str) -> tuple[str, str]:
        """Возвращает (название лиги, страна). Кешируется — соревнования не
        меняются в течение прогона, незачем дёргать API каждый цикл."""
        if competition_id in self._competition_name_cache:
            return self._competition_name_cache[competition_id]
        data, _ = self._get(f"/football/competitions/{competition_id}")
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

        live_data, was_rate_limited = self._get("/football/matches", {"status": "live", "per_page": 50})
        if not live_data:
            if was_rate_limited:
                _log("не удалось получить список live-матчей — лимит выжран, пропускаем цикл")
            else:
                _log("не удалось получить список live-матчей — ошибка запроса, пропускаем цикл")
            return []

        matches_raw = live_data.get("data", [])
        matches_raw = matches_raw[: self.MAX_MATCHES_PER_CYCLE]
        results: list[dict] = []
        consecutive_rate_limits = 0

        for m in matches_raw:
            if consecutive_rate_limits >= MAX_CONSECUTIVE_RATE_LIMITS:
                _log(
                    f"прервано circuit breaker'ом: {consecutive_rate_limits} подряд 429, "
                    f"обработано {len(results)}/{len(matches_raw)} матчей, остальные — в следующий цикл"
                )
                break

            match_id = m.get("id")
            if not match_id:
                continue

            live_stats_data, rl1 = self._get(f"/football/matches/{match_id}/live-stats")
            if not live_stats_data:
                consecutive_rate_limits = consecutive_rate_limits + 1 if rl1 else 0
                continue
            consecutive_rate_limits = 0

            live = live_stats_data.get("data", {})
            meta = live.get("meta", {})
            stats = live.get("stats", {})

            minute = int(meta.get("elapsed_minutes") or 0)

            def _stat(key: str) -> tuple[float, float]:
                block = stats.get(key, {}).get("all", {})
                return float(block.get("home", 0) or 0), float(block.get("away", 0) or 0)

            possession_h, possession_a = _stat("ball_possession")
            shots_on_target_h, shots_on_target_a = _stat("shots_on_target")
            shots_h, shots_a = _stat("total_shots")
            corners_h, corners_a = _stat("corner_kicks")

            # Живой xG — отдельный (дорогой) запрос к shotmap. Дёргаем его
            # только если минута попадает в окно, которое detector реально
            # использует (MIN_USEFUL_MINUTE..MAX_USEFUL_MINUTE) — иначе это
            # трата квоты на матч, который всё равно будет отброшен дальше
            # по пайплайну.
            xg_h, xg_a = 0.0, 0.0
            if MIN_USEFUL_MINUTE <= minute <= MAX_USEFUL_MINUTE:
                shotmap_data, rl2 = self._get(f"/football/matches/{match_id}/shotmap")
                if shotmap_data:
                    consecutive_rate_limits = 0
                    live_xg = shotmap_data.get("np_xg_summary", {}).get("live", {})
                    xg_h = float(live_xg.get("home_team", 0) or 0)
                    xg_a = float(live_xg.get("away_team", 0) or 0)
                elif rl2:
                    consecutive_rate_limits += 1

            competition_id = m.get("competition_id", "")
            league_name, country_name = self._competition_info(competition_id) if competition_id else ("", "")

            home_team = m.get("home_team", {}).get("name", "?")
            away_team = m.get("away_team", {}).get("name", "?")
            score = m.get("score", {}) or {}

            results.append({
                "fixture_id": str(match_id),
                "home_team": home_team,
                "away_team": away_team,
                "minute": minute,
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

        if not results and matches_raw:
            _log(
                f"live-матчей от API получено {len(matches_raw)}, но ни один не дал данных "
                f"(rate limit / ошибки на уровне отдельных матчей — см. логи HTTP выше)"
            )

        return results
