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
    sport_keys: tuple[str, ...]
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
    hours_ahead_limit: float
    oddscorp_auth_key: str
    oddscorp_ws_url: str
    oddscorp_bookmakers: tuple[str, ...]
    league_blacklist: tuple[str, ...]  # подстроки в названии лиги — если совпало, событие выкидывается
    league_whitelist: tuple[str, ...]  # если не пусто — оставляем ТОЛЬКО лиги, где есть совпадение
    sport_whitelist: tuple[str, ...]  # NEW: подстроки в sport_key (напр. football, tennis, basketball).
    # Если не пусто — оставляем ТОЛЬКО котировки, где sport_key содержит одну из подстрок.
    # Оставь пустым (SPORT_WHITELIST="") на первый прогон, чтобы увидеть в debug-логе
    # реальные значения sport_key от OddsCorp и откалибровать список осознанно.

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            odds_api_key=os.getenv("ODDS_API_KEY", "").strip(),
            odds_region=os.getenv("ODDS_REGION", "eu").strip(),
            sport_keys=tuple(
                x.strip() for x in os.getenv("SPORT_KEY", "soccer_brazil_campeonato").split(",") if x.strip()
            ),
            markets=tuple(x.strip() for x in os.getenv("MARKETS", "h2h,totals").split(",") if x.strip()),
            odds_format=os.getenv("ODDS_FORMAT", "decimal").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            database_path=os.getenv("DATABASE_PATH", "data/bbk_scanner.db").strip(),
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
            hours_ahead_limit=float(os.getenv("HOURS_AHEAD_LIMIT", "24")),
            oddscorp_auth_key=os.getenv("ODDSCORP_AUTH_KEY", "").strip(),
            oddscorp_ws_url=os.getenv("ODDSCORP_WS_URL", "ws://api.oddscorp.com:8001").strip(),
            oddscorp_bookmakers=tuple(
                x.strip() for x in os.getenv("ODDSCORP_BOOKMAKERS", "bet365:prematch,parimatch_com:prematch").split(",") if x.strip()
            ),
            league_blacklist=tuple(
                x.strip().lower() for x in os.getenv("LEAGUE_BLACKLIST", "esoccer,replays,e-soccer,simulated").split(",") if x.strip()
            ),
            league_whitelist=tuple(
                x.strip().lower() for x in os.getenv("LEAGUE_WHITELIST", "").split(",") if x.strip()
            ),
            sport_whitelist=tuple(
                x.strip().lower() for x in os.getenv("SPORT_WHITELIST", "").split(",") if x.strip()
            ),
        )
