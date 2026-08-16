"""
v3.0 PRESSURE-детектор — та же логика скоринга, что и pressure_detector.py
(v2.0, Sportmonks), но:
  - версия/метка в сообщении другая (v3.0), без упоминания провайдера —
    конфиденциальность источника данных, как и договаривались
  - отдельный dedup_key (pressure_v3:...), чтобы не конфликтовать с v2.0
    и не подавлять дубли между ветками — это два независимых потока,
    которые мы сознательно сравниваем на этой неделе

Логика намеренно продублирована, а не импортирована с параметром — чтобы
не трогать работающий pressure_detector.py вообще (там 92% winrate, риск
регрессии от рефакторинга не оправдан ради разметки версии).
"""

from __future__ import annotations

from notifier import _detect_country_flag

PRESSURE_DETECTOR_VERSION = "v3.0-2026-08-16"

MIN_MINUTE = 3
MAX_MINUTE = 80

METRIC_SHARE_GAP_THRESHOLD = 15.0
MIN_METRICS_REQUIRED = 2
MIN_COMPOSITE_SCORE = 40.0
PRESSURE_ESCALATION_THRESHOLD = 15.0

METRIC_WEIGHTS = {
    "xg": 0.30,
    "pressure": 0.30,
    "shots_on_target": 0.20,
    "corners": 0.10,
    "possession": 0.10,
}


def _flag_from_iso2(iso2: str | None) -> str:
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        return ""
    iso2 = iso2.upper()
    return "".join(chr(0x1F1E6 + (ord(ch) - ord("A"))) for ch in iso2)


def _share_gap_pct(home_val: float, away_val: float) -> float | None:
    total = home_val + away_val
    if total <= 0:
        return None
    share = home_val / total
    return (share - 0.5) * 2 * 100


def _fmt_stat_line(label: str, home_val: float, away_val: float) -> str | None:
    if home_val == 0 and away_val == 0:
        return None
    if label == "Владение":
        return f"{label}: {home_val:.0f}% — {away_val:.0f}%"
    return f"{label}: {home_val:.0f} — {away_val:.0f}"


def _composite_score(gaps: dict[str, float], dominant_is_home: bool) -> tuple[float, list[str]]:
    weighted_sum = 0.0
    weight_total = 0.0
    agreeing_metrics = []

    for metric, gap in gaps.items():
        if gap is None or abs(gap) < METRIC_SHARE_GAP_THRESHOLD:
            continue
        metric_favors_home = gap > 0
        if metric_favors_home != dominant_is_home:
            continue

        weight = METRIC_WEIGHTS.get(metric, 0.0)
        weighted_sum += weight * abs(gap)
        weight_total += weight
        agreeing_metrics.append(metric)

    if weight_total <= 0:
        return 0.0, agreeing_metrics

    return weighted_sum / weight_total, agreeing_metrics


def detect_pressure_alerts_v3(matches: list[dict]) -> list[dict]:
    """
    matches — результат ThestatsapiPressureProvider.get_live_pressure_data()
    Возвращает список алертов вида:
    [{"dedup_key": str, "message": str, "score": float}, ...]
    """
    print(f"[PRESSURE DETECTOR v3] версия модуля: {PRESSURE_DETECTOR_VERSION}", flush=True)
    if matches:
        m0 = matches[0]
        print(
            f"[PRESSURE DETECTOR v3] пример match keys: league_name={m0.get('league_name')!r} "
            f"country_name={m0.get('country_name')!r}",
            flush=True,
        )

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
            continue

        score, agreeing_metrics = _composite_score(gaps, dominant_is_home)
        if score < MIN_COMPOSITE_SCORE:
            continue

        dominant_team = match["home_team"] if dominant_is_home else match["away_team"]

        home_pressure_total = match.get("home_pressure_total", 0.0)
        away_pressure_total = match.get("away_pressure_total", 0.0)
        pressure_total_gap = _share_gap_pct(home_pressure_total, away_pressure_total)
        pressure_recent_gap = gaps["pressure"]
        is_escalating = False
        if pressure_recent_gap is not None:
            recent_signed = pressure_recent_gap if dominant_is_home else -pressure_recent_gap
            total_signed = (pressure_total_gap if dominant_is_home else -pressure_total_gap) \
                if pressure_total_gap is not None else 0.0
            is_escalating = (recent_signed - total_signed) >= PRESSURE_ESCALATION_THRESHOLD

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

        home_score = match.get("home_score", 0)
        away_score = match.get("away_score", 0)

        country_name = match.get("country_name")
        country_iso2 = match.get("country_iso2")
        league_name_val = match.get("league_name")

        if country_name and country_iso2:
            flag = _flag_from_iso2(country_iso2)
            country_flag = f"{country_name} {flag}" if flag else country_name
        elif country_name:
            country_flag = country_name
        elif league_name_val:
            country_flag = _detect_country_flag(league_name_val)
        else:
            country_flag = ""

        league_line = ""
        if league_name_val:
            league_line = (
                f"🌍 {league_name_val} | {country_flag}"
                if country_flag else f"🌍 {league_name_val}"
            )

        # Метка версии в заголовке — без упоминания провайдера,
        # только чтобы визуально отличать ветку v3.0 от v2.0.
        header = f"📊 xG Статистика v3.0 | {match['home_team']} — {match['away_team']} ({minute}', {home_score}:{away_score})"
        dominance_line = f"Доминирует: {dominant_team} | Сила сигнала: {score:.0f}%"
        if is_escalating:
            dominance_line += " | 🔥 Давление нарастает"

        message_parts = [header]
        if league_line:
            message_parts.append(league_line)
        message_parts.append(dominance_line)
        message_parts.append(" | ".join(reason_parts))
        if extra_lines:
            message_parts.append(" | ".join(extra_lines))

        message = "\n".join(message_parts)

        minute_bucket = (minute // 5) * 5
        dedup_key = f"pressure_v3:{match['fixture_id']}:{minute_bucket}"

        alerts.append({"dedup_key": dedup_key, "message": message, "score": round(score, 1)})

    return alerts
