"""
Генератор карточки-отчёта по матчу для PRESSURE-алертов.

Рисует график нарастания давления (Pressure Index за скользящее 15-минутное
окно, xG) по истории, накопленной в pressure_snapshots, и накладывает
текстовый блок с командами, BBK Score и статусом.

Используется только matplotlib (Agg-backend, без дисплея) — не нужен
отдельный headless-браузер, что важно на бесплатном/hobby тарифе Railway.

ВАЖНО: перед первым запуском нужно добавить matplotlib в requirements.txt
(pip install matplotlib на Railway произойдёт автоматически при следующем
деплое, если строка там есть).

Цвета — навy/gold/cream, как в остальном брендинге BBK (WC2026 карточки).
Если у тебя сохранены точные hex-коды бренда — просто замени константы ниже,
сейчас это приближение по памяти.
"""

from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

BRAND_NAVY = "#0B1F3A"
BRAND_GOLD = "#C9A227"
BRAND_CREAM = "#F5F0E1"
BRAND_RED = "#B8322B"


def _parse_ts(captured_at: str) -> datetime:
    return datetime.fromisoformat(captured_at.replace("Z", "+00:00"))


def generate_pressure_card(
    match: dict,
    history: list[dict],
    bbk_score: float,
    is_escalating: bool = False,
) -> bytes:
    """
    match — один normalized-словарь из sportmonks_provider (текущий снимок)
    history — результат db.get_pressure_history(fixture_id), отсортирован
              по возрастанию captured_at
    bbk_score — score алерта (0-100), из detect_pressure_alerts
    is_escalating — True если сработала пометка "🔥 Давление нарастает"

    Возвращает PNG как bytes — готово для notifier.send_photo().
    """
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    fig.patch.set_facecolor(BRAND_NAVY)
    ax.set_facecolor(BRAND_NAVY)

    if history:
        timestamps = [_parse_ts(row["captured_at"]) for row in history]
        home_pressure = [row.get("home_pressure") or 0 for row in history]
        away_pressure = [row.get("away_pressure") or 0 for row in history]

        ax.plot(
            timestamps, home_pressure,
            color=BRAND_GOLD, linewidth=2.5, marker="o", markersize=3,
            label=match.get("home_team", "Хозяева"),
        )
        ax.plot(
            timestamps, away_pressure,
            color=BRAND_CREAM, linewidth=2.0, marker="o", markersize=3,
            label=match.get("away_team", "Гости"),
        )
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.legend(
            loc="upper left", facecolor=BRAND_NAVY, edgecolor=BRAND_GOLD,
            labelcolor=BRAND_CREAM, fontsize=9,
        )
    else:
        ax.text(
            0.5, 0.5, "Недостаточно истории для графика",
            ha="center", va="center", color=BRAND_CREAM, fontsize=11,
            transform=ax.transAxes,
        )

    ax.tick_params(colors=BRAND_CREAM, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(BRAND_GOLD)
    ax.set_ylabel("Pressure Index (15м окно)", color=BRAND_CREAM, fontsize=9)

    status_text = "ДАВЛЕНИЕ НАРАСТАЕТ" if is_escalating else "PRESSURE СИГНАЛ"
    status_color = BRAND_RED if is_escalating else BRAND_GOLD

    title = f"{match.get('home_team', '')} — {match.get('away_team', '')}"
    subtitle = (
        f"{match.get('minute', 0)}' | "
        f"{match.get('home_score', 0)}:{match.get('away_score', 0)} | "
        f"BBK Score: {bbk_score:.0f}/100"
    )

    fig.text(0.5, 0.97, status_text, ha="center", color=status_color,
              fontsize=13, fontweight="bold")
    fig.text(0.5, 0.925, title, ha="center", color=BRAND_CREAM, fontsize=12)
    fig.text(0.5, 0.885, subtitle, ha="center", color=BRAND_GOLD, fontsize=10)

    league_name = match.get("league_name")
    if league_name:
        fig.text(0.5, 0.85, league_name, ha="center", color=BRAND_CREAM,
                  fontsize=8, alpha=0.8)

    fig.text(0.5, 0.03, "Ordo Ex Disciplina · BBK Scanner", ha="center",
              color=BRAND_GOLD, fontsize=8, alpha=0.7)

    plt.subplots_adjust(top=0.78, bottom=0.12)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()
