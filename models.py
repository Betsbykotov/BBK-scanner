from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class OddsQuote:
    event_id: str
    sport_key: str
    commence_time: str
    home_team: str
    away_team: str
    bookmaker_key: str
    bookmaker_title: str
    market_key: str
    outcome_name: str
    point: float | None
    price: float
    captured_at: str
    league_name: str = ""  # NEW: имя лиги/турнира, если провайдер его отдаёт (пусто, если нет)

    @property
    def selection_key(self) -> str:
        point = "" if self.point is None else f":{self.point:g}"
        return f"{self.event_id}:{self.market_key}:{self.outcome_name}{point}"

    @property
    def is_live(self) -> bool:
        """Матч уже начался на момент снятия этой котировки (LIVE), а не прематч."""
        try:
            commence = datetime.fromisoformat(self.commence_time.replace("Z", "+00:00"))
            captured = datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return captured >= commence

    @classmethod
    def now(cls, **kwargs) -> "OddsQuote":
        return cls(captured_at=datetime.now(timezone.utc).isoformat(), **kwargs)


@dataclass(frozen=True)
class Alert:
    quote: OddsQuote
    previous_price: float | None
    movement_pct: float | None
    market_average: float | None
    market_deviation_pct: float | None
    reason: str
    bbk_score: float = 0.0
    bbk_tier: str = ""
    velocity_pct_per_min: float | None = None
    consensus_pct: float = 0.0
    is_sharp_source: bool = False
    signal_type: str = "STANDARD"  # NEW: "STANDARD" | "MOMENTUM"

    @property
    def dedup_key(self) -> str:
        """Ключ дедупликации: конкретная линия у конкретного букмекера.

        Без bookmaker_key в ключе алерты от разных БК по одному и тому же
        событию гасили бы друг друга — а это разные сигналы.
        """
        return f"{self.quote.selection_key}:{self.quote.bookmaker_key}:{self.signal_type}"
