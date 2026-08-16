from __future__ import annotations

from datetime import datetime, timezone
import json
import urllib.parse
import urllib.request

from models import OddsQuote
from providers import OddsProvider, ProviderError


def _american_to_decimal(american: float) -> float:
    """Конвертирует американские коэффициенты SportsGameOdds ('-115', '+230')
    в десятичный формат, совместимый с остальным пайплайном (OddsCorp/analyzer
    работают с decimal-ценами вида 1.85, 2.40 и т.д.)."""
    if american > 0:
        return 1.0 + (american / 100.0)
    return 1.0 + (100.0 / abs(american))


class SportsGameOddsProvider(OddsProvider):
    BASE_URL = "https://api.sportsgameodds.com/v2"

    # Полный матч (регулярное время) — маркеты 1x2/тоталов, которые нужны
    # SHARP/MOMENTUM. 1st half, player props, corners/cards и т.п. — режутся.
    _FULL_MATCH_PERIODS = ("reg", "game")
    _H2H_BET_TYPES = ("ml", "ml3way")
    _H2H_SIDES = ("home", "away", "draw")
    _TOTALS_BET_TYPES = ("ou",)
    _TOTALS_SIDES = ("over", "under")

    def __init__(
        self,
        api_key: str,
        sport_id: str = "SOCCER",
        league_ids: tuple[str, ...] = (),
        max_pages: int = 10,
        page_limit: int = 100,
    ):
        if not api_key:
            raise ProviderError("SPORTSGAMEODDS_API_KEY не заполнен.")
        self.api_key = api_key
        self.sport_id = sport_id
        self.league_ids = league_ids
        self.max_pages = max_pages
        self.page_limit = page_limit

    def _get_json(self, params: dict[str, str]) -> dict:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self.BASE_URL}/events/?{query}",
            headers={"User-Agent": "BBK-Scanner-MVP/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ProviderError(f"Ошибка API SportsGameOdds: {exc}") from exc

    def _fetch_events(self) -> list[dict]:
        base_params: dict[str, str] = {
            "apiKey": self.api_key,
            "oddsAvailable": "true",
            "limit": str(self.page_limit),
        }
        if self.league_ids:
            base_params["leagueID"] = ",".join(self.league_ids)
        elif self.sport_id:
            base_params["sportID"] = self.sport_id

        events: list[dict] = []
        cursor: str | None = None
        for _ in range(self.max_pages):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            payload = self._get_json(params)
            if not payload.get("success", True):
                raise ProviderError(f"SportsGameOdds вернул success=false: {payload}")
            page = payload.get("data") or []
            events.extend(page)
            cursor = payload.get("nextCursor")
            if not cursor:
                break
        return events

    @staticmethod
    def _team_name(event: dict, side: str) -> str:
        team = (event.get("teams") or {}).get(side) or {}
        names = team.get("names") or {}
        return str(names.get("medium") or names.get("long") or names.get("short") or "")

    def _parse_odd_object(
        self,
        odd: dict,
        home: str,
        away: str,
    ) -> tuple[str, str, float | None] | None:
        """Возвращает (market_key, outcome_name, point) для нужных нам маркетов,
        либо None, если это prop/угловые/карточки/половины и т.п. — их не берём."""
        period = odd.get("periodID", "")
        if period not in self._FULL_MATCH_PERIODS:
            return None

        bet_type = odd.get("betTypeID", "")
        side = odd.get("sideID", "")

        if bet_type in self._H2H_BET_TYPES and side in self._H2H_SIDES:
            outcome_name = {"home": home, "away": away, "draw": "Draw"}[side]
            return "h2h", outcome_name, None

        if bet_type in self._TOTALS_BET_TYPES and side in self._TOTALS_SIDES:
            if odd.get("statEntityID") != "all":
                return None  # тоталы конкретной команды нам не нужны
            outcome_name = side.capitalize()
            raw_point = odd.get("bookOverUnder") or odd.get("fairOverUnder")
            point = None
            if raw_point is not None:
                try:
                    point = float(raw_point)
                except (TypeError, ValueError):
                    point = None
            return "totals", outcome_name, point

        return None

    def fetch(self) -> list[OddsQuote]:
        events = self._fetch_events()
        captured_at = datetime.now(timezone.utc).isoformat()
        quotes: list[OddsQuote] = []

        for event in events:
            odds_map = event.get("odds") or {}
            if not odds_map:
                continue

            home = self._team_name(event, "home")
            away = self._team_name(event, "away")
            if not home or not away:
                continue

            sport_key = str(event.get("sportID", "")).lower()
            league_name = str(event.get("leagueID", ""))
            status = event.get("status") or {}
            commence_time = str(status.get("startsAt", ""))
            event_id = str(event.get("eventID", ""))

            for odd in odds_map.values():
                parsed = self._parse_odd_object(odd, home, away)
                if parsed is None:
                    continue
                market_key, outcome_name, point = parsed

                by_bookmaker = odd.get("byBookmaker") or {}
                for bk_id, bk_data in by_bookmaker.items():
                    if not bk_data.get("available", False):
                        continue
                    raw_odds = bk_data.get("odds")
                    if raw_odds is None:
                        continue
                    try:
                        american = float(str(raw_odds).replace("+", ""))
                    except (TypeError, ValueError):
                        continue
                    price = _american_to_decimal(american)

                    bk_point = point
                    if market_key == "totals" and "overUnder" in bk_data:
                        try:
                            bk_point = float(bk_data["overUnder"])
                        except (TypeError, ValueError):
                            pass

                    quotes.append(
                        OddsQuote(
                            event_id=event_id,
                            sport_key=sport_key,
                            commence_time=commence_time,
                            home_team=home,
                            away_team=away,
                            bookmaker_key=str(bk_id),
                            bookmaker_title=str(bk_id),
                            market_key=market_key,
                            outcome_name=outcome_name,
                            point=bk_point,
                            price=round(price, 3),
                            captured_at=captured_at,
                            league_name=league_name,
                        )
                    )
        return quotes
