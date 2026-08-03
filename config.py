import os


def _env(key, default=None):
    val = os.environ.get(key, default)
    if val is not None:
        val = val.strip()
    return val


def _env_int(key, default):
    val = _env(key)
    return int(val) if val not in (None, "") else default


def _env_float(key, default):
    val = _env(key)
    return float(val) if val not in (None, "") else default


def _env_list(key, default=None):
    val = _env(key)
    if not val:
        return default or []
    return [item.strip() for item in val.split(",") if item.strip()]


# --- Telegram ---
BOT_TOKEN = _env("BOT_TOKEN")
CHAT_ID = _env("CHAT_ID")

# --- Scanner thresholds ---
MOVEMENT_THRESHOLD = _env_float("MOVEMENT", 12)        # % change in odds vs first-seen
DEVIATION_THRESHOLD = _env_float("DEVIATION", 10)      # % deviation vs other bookmakers
MIN_SCORE_TO_NOTIFY = _env_float("MIN_SCORE_TO_NOTIFY", 60)
COOLDOWN_SECONDS = _env_int("COOLDOWN", 90)

# --- The Odds API (REST, fallback/parallel path) ---
ODDS_API_KEY = _env("ODDS_API_KEY")
ODDS_REGION = _env("ODDS_REGION", "eu")
SPORT_KEYS = _env_list("SPORT_KEY", ["soccer_epl"])
POLL_INTERVAL_MINUTES = _env_int("POLL_INTERVAL_MINUTES", 240)

# --- ODDSCORP (WebSocket, prematch, Bet365 + Parimatch trial) ---
ODDSCORP_TOKEN = _env("ODDSCORP_TOKEN")
ODDSCORP_WS_URL = _env("ODDSCORP_WS_URL", "ws://api.oddscorp.com:8001")
ODDSCORP_BOOKMAKERS = _env_list("ODDSCORP_BOOKMAKERS", ["bet365", "parimatch_com"])
ODDSCORP_MODE = _env("ODDSCORP_MODE", "prematch")
