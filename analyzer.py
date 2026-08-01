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
        score_weights: BBKScoreWeights = BBKScoreWeights(),
    ):
        self.db = db
        self.movement_threshold_pct = movement_threshold_pct
        self.market_deviation_threshold_pct = market_deviation_threshold_pct
        self.lookback_minutes = lookback_minutes
        self.min_bookmakers = min_bookmakers
        self.velocity_threshold_pct_per_min = velocity_threshold_pct_per_min
        self.sharp_bookmakers = set(sharp_bookmakers)
        self.sharp_bonus_multiplier = sharp_bonus_multiplier
        self.score_weights = score_weights

    def analyze(self, quotes: list[OddsQuote]) -> list[Alert]:
        grouped: dict[str, list[OddsQuote]] = defaultdict(list)
        for quote in quotes:
            grouped[quote.selection_key].append(quote)

        now_iso = datetime.now(timezone.utc).isoformat()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=self.lookback_minutes)
        ).isoformat()

        alerts: list[Alert] = []
        for group in grouped.values():
            market_average = (
                sum(q.price for q in group) / len(group)
                if len({q.bookmaker_key for q in group}) >= self.min_bookmakers
                else None
            )

            # Первый проход по группе: считаем движение и скорость для КАЖДОЙ
            # котировки. Это нужно заранее, чтобы на втором проходе посчитать
            # consensus — сколько других БК в этой же группе двигались в ту
            # же сторону (лайт-версия steam move detection).
            movements: dict[str, float | None] = {}
            velocities: dict[str, float | None] = {}
            previous_map: dict[str, float | None] = {}
            for quote in group:
                prev = self.db.previous_quote(quote, cutoff)
                if prev is not None:
                    prev_price, prev_captured_at = prev
                    movements[quote.bookmaker_key] = pct_change(prev_price, quote.price)
                    elapsed = _elapsed_minutes(prev_captured_at, now_iso)
                    velocities[quote.bookmaker_key] = (
                        abs(movements[quote.bookmaker_key]) / elapsed if elapsed > 0 else None
                    )
                    previous_map[quote.bookmaker_key] = prev_price
                else:
                    movements[quote.bookmaker_key] = None
                    velocities[quote.bookmaker_key] = None
                    previous_map[quote.bookmaker_key] = None

            for quote in group:
                previous = previous_map[quote.bookmaker_key]
                movement = movements[quote.bookmaker_key]
                velocity = velocities[quote.bookmaker_key]
                deviation = (
                    pct_change(market_average, quote.price)
                    if market_average is not None
                    else None
                )

                reasons: list[str] = []
                if movement is not None and abs(movement) >= self.movement_threshold_pct:
                    reasons.append(
                        f"движение {movement:+.2f}% за период до {self.lookback_minutes} мин."
                    )
                if deviation is not None and abs(deviation) >= self.market_deviation_threshold_pct:
                    reasons.append(f"отклонение от рынка {deviation:+.2f}%")

                if not reasons:
                    continue

                # Consensus: доля ДРУГИХ букмекеров в группе, которые двигались
                # в ту же сторону, что и текущая котировка.
                same_direction = 0
                other_count = 0
                if movement is not None:
                    direction = 1 if movement > 0 else -1
                    for book_key, other_movement in movements.items():
                        if book_key == quote.bookmaker_key or other_movement is None:
                            continue
                        other_count += 1
                        other_direction = 1 if other_movement > 0 else -1
                        if other_direction == direction:
                            same_direction += 1
                consensus_pct = (same_direction / other_count * 100.0) if other_count else 0.0

                is_sharp = quote.bookmaker_key in self.sharp_bookmakers
                score, tier = compute_bbk_score(
                    BBKScoreInputs(
                        movement_pct=movement,
                        movement_threshold_pct=self.movement_threshold_pct,
                        deviation_pct=deviation,
                        deviation_threshold_pct=self.market_deviation_threshold_pct,
                        velocity_pct_per_min=velocity,
                        velocity_threshold_pct_per_min=self.velocity_threshold_pct_per_min,
                        consensus_pct=consensus_pct,
                        is_sharp_source=is_sharp,
                        sharp_bonus_multiplier=self.sharp_bonus_multiplier,
                    ),
                    self.score_weights,
                )

                alerts.append(
                    Alert(
                        quote=quote,
                        previous_price=previous,
                        movement_pct=movement,
                        market_average=market_average,
                        market_deviation_pct=deviation,
                        reason="; ".join(reasons),
                        bbk_score=score,
                        bbk_tier=tier,
                        velocity_pct_per_min=velocity,
                        consensus_pct=round(consensus_pct, 1),
                        is_sharp_source=is_sharp,
                    )
                )

        alerts.sort(key=lambda a: a.bbk_score, reverse=True)
        return alerts
