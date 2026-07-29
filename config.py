from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    odds_api_key: str
    odds_region: str
    sport_key: str
    markets: tuple[str, ...]
    odds_format: str
    telegram_bot_token: str
    telegram_chat_id: str
    database_path: str
    movement_threshold_pct: float
    market_deviation_threshold_pct: float
    lookback_minutes: int
    min_bookmakers: int
    velocity_threshold_pct_per_min: float
    sharp_bookmakers: tuple[str, ...]
    sharp_bonus_multiplier: float
    cooldown_minutes: int
    poll_interval_minutes: int
    min_score_to_notify: float

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            odds_api_key=os.getenv("ODDS_API_KEY", ""),
            odds_region=os.getenv("ODDS_REGION", "eu"),
            sport_key=os.getenv("SPORT_KEY", "soccer_uefa_champs_league"),
            markets=tuple(x.strip() for x in os.getenv("MARKETS", "h2h,totals").split(",") if x.strip()),
            odds_format=os.getenv("ODDS_FORMAT", "decimal"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            database_path=os.getenv("DATABASE_PATH", "data/bbk_scanner.db"),
            movement_threshold_pct=float(os.getenv("MOVEMENT_THRESHOLD_PCT", "8")),
            market_deviation_threshold_pct=float(os.getenv("MARKET_DEVIATION_THRESHOLD_PCT", "6")),
            lookback_minutes=int(os.getenv("LOOKBACK_MINUTES", "30")),
            min_bookmakers=int(os.getenv("MIN_BOOKMAKERS", "2")),
            velocity_threshold_pct_per_min=float(os.getenv("VELOCITY_THRESHOLD_PCT_PER_MIN", "0.5")),
            sharp_bookmakers=tuple(
                x.strip() for x in os.getenv("SHARP_BOOKMAKERS", "pinnacle").split(",") if x.strip()
            ),
            sharp_bonus_multiplier=float(os.getenv("SHARP_BONUS_MULTIPLIER", "1.15")),
            cooldown_minutes=int(os.getenv("COOLDOWN_MINUTES", "60")),
            poll_interval_minutes=int(os.getenv("POLL_INTERVAL_MINUTES", "5")),
            min_score_to_notify=float(os.getenv("MIN_SCORE_TO_NOTIFY", "0")),
        )
