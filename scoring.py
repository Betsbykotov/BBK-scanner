from __future__ import annotations

from dataclasses import dataclass


# Тиры BBK Score — от них зависит, как оформляется алерт и стоит ли
# вообще присылать его пользователю (например, в будущем можно резать
# алерты с score < 40 без уведомления в Telegram).
TIERS: tuple[tuple[float, str], ...] = (
    (80.0, "🔥 ТОП"),
    (60.0, "⚡ Сильный"),
    (40.0, "👀 Средний"),
    (0.0, "· Слабый"),
)


def classify_tier(score: float) -> str:
    for cutoff, label in TIERS:
        if score >= cutoff:
            return label
    return TIERS[-1][1]


@dataclass(frozen=True)
class BBKScoreWeights:
    """Веса компонентов BBK Score. Сумма должна быть равна 1.0.

    movement    — сила изменения цены относительно прошлого снимка
    deviation   — отклонение от среднего по рынку (пока без де-вига,
                  это следующий этап, скоринг уже готов его принять)
    velocity    — скорость движения (%/мин) — быстрый скачок весомее,
                  чем такое же по размеру, но растянутое на час дрожание
    consensus   — сколько других БК в той же группе двигались туда же —
                  это лайт-версия steam move detection
    """

    movement: float = 0.30
    deviation: float = 0.25
    velocity: float = 0.25
    consensus: float = 0.20


@dataclass(frozen=True)
class BBKScoreInputs:
    movement_pct: float | None
    movement_threshold_pct: float
    deviation_pct: float | None
    deviation_threshold_pct: float
    velocity_pct_per_min: float | None
    velocity_threshold_pct_per_min: float
    consensus_pct: float  # 0..100, доля БК в группе, двигавшихся в ту же сторону
    is_sharp_source: bool
    sharp_bonus_multiplier: float


def _scale(value: float | None, threshold: float, saturate_at_x_threshold: float = 2.5) -> float:
    """Переводит сырое значение в шкалу 0..100.

    Значение = threshold -> 50 баллов (то есть ровно на грани алерта).
    Значение = threshold * saturate_at_x_threshold -> 100 баллов (максимум).
    Это не линейная привязка "просто к порогу", а привязка к тому, НАСКОЛЬКО
    сильно порог превышен — так топовые движения реально выделяются на фоне
    пограничных.
    """
    if value is None or threshold <= 0:
        return 0.0
    ratio = abs(value) / threshold
    score = (ratio / saturate_at_x_threshold) * 100.0
    return max(0.0, min(100.0, score))


def compute_bbk_score(inputs: BBKScoreInputs, weights: BBKScoreWeights = BBKScoreWeights()) -> tuple[float, str]:
    movement_score = _scale(inputs.movement_pct, inputs.movement_threshold_pct)
    deviation_score = _scale(inputs.deviation_pct, inputs.deviation_threshold_pct)
    velocity_score = _scale(inputs.velocity_pct_per_min, inputs.velocity_threshold_pct_per_min)
    consensus_score = max(0.0, min(100.0, inputs.consensus_pct))

    raw = (
        movement_score * weights.movement
        + deviation_score * weights.deviation
        + velocity_score * weights.velocity
        + consensus_score * weights.consensus
    )

    if inputs.is_sharp_source:
        raw *= inputs.sharp_bonus_multiplier

    score = round(max(0.0, min(100.0, raw)), 1)
    return score, classify_tier(score)
