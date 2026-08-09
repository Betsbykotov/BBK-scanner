"""
Детектор xG / Pressure Index дисбаланса на основе данных Sportmonks.

Полностью отдельный модуль — не трогает analyzer.py и никак не пересекается
с MOMENTUM/SHARP логикой на коэффициентах. Работает только с live-статистикой
самого матча.

Логика (v2, 09.08.2026): сравниваем не абсолютную разницу между командами
(она зависит от масштаба конкретной метрики и легко даёт ложные 100%-сигналы
на шумных данных), а ДОЛЮ каждой команды в метрике — она одинаково работает
для Pressure Index (сотни/тысячи), xG (единицы) и статистики (единицы/проценты).

Алерт заводится, только если минимум MIN_METRICS_REQUIRED независимых метрик
согласованно указывают на одну и ту же команду с заметным перекосом — одна
"кричащая" метрика при остальных ровных больше не считается сигналом.
"""

from __future__ import annotations

MIN_MINUTE = 15                 # не проверяем матчи в первые 15 минут (мало данных)
MAX_MINUTE = 80                 # после 80-й минуты сигнал уже малополезен

# Порог "заметного перекоса" в долях (%) — ниже него метрика считается ровной
# и не участвует ни в подсчёте согласованности, ни в итоговом score.
METRIC_SHARE_GAP_THRESHOLD = 15.0

# Минимум метрик, согласованно указывающих на одну команду, чтобы завести алерт.
MIN_METRICS_REQUIRED = 2

# Итоговый composite score ниже этого — сигнал слишком слабый, не алертим.
MIN_COMPOSITE_SCORE = 40.0

# Вес каждой метрики в итоговом score.
METRIC_WEIGHTS = {
    "xg": 0.30,
    "pressure": 0.30,
    "shots_on_target": 0.20,
    "corners": 0.10,
    "possession": 0.10,
}


def _share_gap_pct(home_val: float, away_val: float) -> float | None:
    """
    Перекос доли команды-хозяев в метрике, в процентных пунктах от 50/50.
    +100 — вся метрика у хозяев, -100 — вся у гостей, 0 — идеально поровну.
    None — если обе величины нулевые (метрики нет / она не применима).
    """
    total = home_val + away_val
    if total <= 0:
        return None
    share = home_val / total
    return (share - 0.5) * 2 * 100


def _fmt_stat_line(label: str, home_val: float, away_val: float) -> str | None:
    """Возвращает строку статистики, только если хотя бы одно значение не нулевое."""
    if home_val == 0 and away_val == 0:
        return None
    if label == "Владение":
        return f"{label}: {home_val:.0f}% — {away_val:.0f}%"
    return f"{label}: {home_val:.0f} — {away_val:.0f}"


def _composite_score(gaps: dict[str, float], dominant_is_home: bool) -> tuple[float, list[str]]:
    """
    Взвешенное среднее по метрикам, которые СОГЛАСУЮТСЯ с доминирующей командой.
    Метрика "согласуется", если её gap по знаку и модулю (>= порога) указывает
    на ту же команду, что и итоговый dominant_team.

    Возвращает (score 0-100, список имён метрик, вошедших в подсчёт).
    """
    weighted_sum = 0.0
    weight_total = 0.0
    agreeing_metrics = []

    for metric, gap in gaps.items():
        if gap is None or abs(gap) < METRIC_SHARE_GAP_THRESHOLD:
            continue
        metric_favors_home = gap > 0
        if metric_favors_home != dominant_is_home:
            continue  # метрика указывает на другую команду — не учитываем в score

        weight = METRIC_WEIGHTS.get(metric, 0.0)
        weighted_sum += weight * abs(gap)
        weight_total += weight
        agreeing_metrics.append(metric)

    if weight_total <= 0:
        return 0.0, agreeing_metrics

    return weighted_sum / weight_total, agreeing_metrics


def detect_pressure_alerts(matches: list[dict]) -> list[dict]:
    """
    matches — результат SportmonksProvider.get_live_pressure_data()

    Возвращает список алертов вида:
    [{"dedup_key": str, "message": str, "score": float}, ...]

    "score" нужен main.py для сортировки при срезе часовым лимитом —
    чтобы при MAX_ALERTS_PER_HOUR в первую очередь уходили самые
    убедительные сигналы, а не первые попавшиеся по порядку.
    """
    alerts = []

    for match in matches:
        minute = match.get("minute", 0)
        if minute < MIN_MINUTE or minute > MAX_MINUTE:
            continue

        home_xg = match.get("home_xg", 0.0)
        away_xg = match.get("away_xg", 0.0)
        home_pressure = match.get("home_pressure", 0.0)
        away_pressure = match.get("away_pressure", 0.0)
        home_shots_on_target = match.get("home_shots_on_target", 0.0)
        away_shots_on_target = match.get("away_shots_on_target", 0.0)
        home_corners = match.get("home_corners", 0.0)
        away_corners = match.get("away_corners", 0.0)
        home_possession = match.get("home_possession", 0.0)
        away_possession = match.get("away_possession", 0.0)

        gaps = {
            "xg": _share_gap_pct(home_xg, away_xg),
            "pressure": _share_gap_pct(home_pressure, away_pressure),
            "shots_on_target": _share_gap_pct(home_shots_on_target, away_shots_on_target),
            "corners": _share_gap_pct(home_corners, away_corners),
            "possession": _share_gap_pct(home_possession, away_possession),
        }

        home_votes = sum(1 for g in gaps.values() if g is not None and g >= METRIC_SHARE_GAP_THRESHOLD)
        away_votes = sum(1 for g in gaps.values() if g is not None and g <= -METRIC_SHARE_GAP_THRESHOLD)

        if home_votes >= MIN_METRICS_REQUIRED and home_votes > away_votes:
            dominant_is_home = True
        elif away_votes >= MIN_METRICS_REQUIRED and away_votes > home_votes:
            dominant_is_home = False
        else:
            continue  # недостаточно согласованных метрик — пропускаем, это шум

        score, agreeing_metrics = _composite_score(gaps, dominant_is_home)
        if score < MIN_COMPOSITE_SCORE:
            continue

        dominant_team = match["home_team"] if dominant_is_home else match["away_team"]

        metric_labels = {
            "xg": f"xG {home_xg:.2f}-{away_xg:.2f}",
            "pressure": f"Pressure(15м) {home_pressure:.0f}-{away_pressure:.0f}",
            "shots_on_target": f"Удары в створ {home_shots_on_target:.0f}-{away_shots_on_target:.0f}",
            "corners": f"Угловые {home_corners:.0f}-{away_corners:.0f}",
            "possession": f"Владение {home_possession:.0f}%-{away_possession:.0f}%",
        }
        reason_parts = [metric_labels[m] for m in agreeing_metrics]

        extra_lines = []
        for label, home_key, away_key in (
            ("Удары", "home_shots", "away_shots"),
            ("Удары в створ", "home_shots_on_target", "away_shots_on_target"),
            ("Угловые", "home_corners", "away_corners"),
            ("Владение", "home_possession", "away_possession"),
        ):
            line = _fmt_stat_line(label, match.get(home_key, 0.0), match.get(away_key, 0.0))
            if line:
                extra_lines.append(line)

        home_pressure_total = match.get("home_pressure_total", 0.0)
        away_pressure_total = match.get("away_pressure_total", 0.0)

        message_parts = [
            f"📊 xG Статистика | {match['home_team']} — {match['away_team']} ({minute}')",
            f"Доминирует: {dominant_team} | Сила сигнала: {score:.0f}%",
            " | ".join(reason_parts),
        ]
        if home_pressure_total or away_pressure_total:
            message_parts.append(f"Pressure за матч: {home_pressure_total:.0f}-{away_pressure_total:.0f}")
        if extra_lines:
            message_parts.append(" | ".join(extra_lines))

        message = "\n".join(message_parts)

        minute_bucket = (minute // 5) * 5
        dedup_key = f"pressure:{match['fixture_id']}:{minute_bucket}"

        alerts.append({"dedup_key": dedup_key, "message": message, "score": round(score, 1)})

    return alerts
