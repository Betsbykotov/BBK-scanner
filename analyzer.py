from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from database import OddsDatabase
from models import Alert, OddsQuote
from scoring import BBKScoreInputs, BBKScoreWeights, compute_bbk_score


def pct_change(old: float, new: float) -> float:
    if old <= 0:
        raise ValueError("Старый коэффициент должен быть больше нуля.")
    return (new - old) / old * 100.0


def _elapsed_minutes(previous_iso: str, current_iso: str) -> float:
    previous = datetime.fromisoformat(previous_iso)
    current = datetime.fromisoformat(current_iso)
    delta = (current - previous).total_seconds() / 60.0
    return delta if delta > 0 else 0.0


class OddsAnalyzer:
    def __init__(
        self,
        db: OddsDatabase,
        movement_threshold_pct: float,
        market_deviation_threshold_pct: float,
        lookback_minutes: int,
        min_bookmakers: int,
        velocity_threshold_pct_per_min: float = 0.5,
        sharp_bookmakers: tuple[str, ...] = ("pinnacle",),
        sharp_bonus_multiplier: float = 1.15,
        score
