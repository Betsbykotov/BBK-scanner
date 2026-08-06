"""
Детектор xG / Pressure Index дисбаланса на основе данных Sportmonks.

Полностью отдельный модуль — не трогает analyzer.py и никак не пересекается
с MOMENTUM/SHARP логикой на коэффициентах. Работает только с live-статистикой
самого матча.
"""

from __future__ import annotations

# Пороги можно вынести в config.py позже, пока — константы для простоты.
MIN_MINUTE = 15                 # не проверяем матчи в первые 15 минут (мало данных)
MAX_MINUTE = 80                 # после 80-й минуты сигнал уже малополезен
XG_GAP_THRESHOLD = 0.8          # разница xG между командами
PRESSURE_GAP_THRESHOLD = 15.0   # разница Pressure Index между командами

# Для расчёта "силы сигнала" (%) — на сколько порогов перекрыт разрыв.
# Если разрыв ровно на пороге — это 50%, если в 2 раза больше порога — 100% (кап).
XG_GAP_FOR_100_PCT = XG_GAP_THRESHOLD * 2.0
PRESSURE_GAP_FOR_100_PCT = PRESSURE_GAP_THRESHOLD * 2.0


def _confidence_pct(xg_gap: float, pressure_gap: float) -> int:
    """
    Простая, прозрачная оценка "силы сигнала" в процентах (0-100).
    Не чёрный ящик: берём максимум из двух нормализованных разрывов.
    50% — разрыв ровно на пороге срабатывания, 100% — разрыв в 2+ раза больше порога.
    """
    xg_score = min(abs(xg_gap) / XG_GAP_FOR_100_PCT, 1.0) * 100
    pressure_score = min(abs(pressure_gap) / PRESSURE_GAP_FOR_100_PCT, 1.0) * 100
    return round(max(xg_score, pressure_score))


def _fmt_stat_line(label: str, home_val: float, away_val: float) -> str | None:
    """Возвращает строку статистики, только если хотя бы одно значение не нулевое."""
    if home_val == 0 and away_val == 0:
        return None
    if label == "Владение":
        return f"{label}: {home_val:.0f}% — {away_val:.0f}%"
    return f"{label}: {home_val:.0f} — {away_val:.0f}"


def detect_pressure_alerts(matches: list[dict]) -> list[dict]:
    """
    matches — результат SportmonksProvider.get_live_pressure_data()

    Возвращает список алертов вида:
    [{"dedup_key": str, "message": str}, ...]

    Логика простая и явная (не чёрный ящик): смотрим на явный дисбаланс
    xG и/или Pressure Index между командами в разумном игровом окне.
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

        xg_gap = home_xg - away_xg
        pressure_gap = home_pressure - away_pressure

        dominant_team = None
        reason_parts = []

        if abs(xg_gap) >= XG_GAP_THRESHOLD:
            dominant_team = match["home_team"] if xg_gap > 0 else match["away_team"]
            reason_parts.append(f"xG {home_xg:.2f}-{away_xg:.2f}")

        if abs(pressure_gap) >= PRESSURE_GAP_THRESHOLD:
            pressure_team = match["home_team"] if pressure_gap > 0 else match["away_team"]
            reason_parts.append(f"Pressure {home_pressure:.0f}-{away_pressure:.0f}")
            # Если xG и pressure указывают на одну и ту же команду — это сильнее,
            # но даже одного условия достаточно, чтобы завести алерт.
            if dominant_team is None:
                dominant_team = pressure_team

        if dominant_team is None:
            continue

        confidence = _confidence_pct(xg_gap, pressure_gap)

        # Доп. статистика — только для наглядности, не влияет на срабатывание алерта.
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

        message_parts = [
            f"📊 xG Статистика | {match['home_team']} — {match['away_team']} ({minute}')",
            f"Доминирует: {dominant_team} | Сила сигнала: {confidence}%",
            " | ".join(reason_parts),
        ]
        if extra_lines:
            message_parts.append(" | ".join(extra_lines))

        message = "\n".join(message_parts)

        # dedup по fixture_id + минуте (округлённой до 5 мин), чтобы не спамить
        # каждую минуту одним и тем же дисбалансом
        minute_bucket = (minute // 5) * 5
        dedup_key = f"pressure:{match['fixture_id']}:{minute_bucket}"

        alerts.append({"dedup_key": dedup_key, "message": message})

    return alerts
