import os
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")


def _id(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value.isdigit() else None


TOKEN = os.getenv("DISCORD_TOKEN")
# Optional: a test server where slash commands appear instantly (empty = global sync)
DEV_GUILD_ID = _id("DEV_GUILD_ID")

# ---- internal API for the PHP panel ----
# The panel calls the bot with this secret. Same value goes to panel/config.php -> bot_api_secret.
API_SECRET = os.getenv("API_SECRET", "").strip()
# Keep 127.0.0.1 when the panel runs on the same server (recommended)
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8081"))

# ---- inventory checker ----
# https://steamcommunity.com/dev/apikey — profile names, countries, /id/ links
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "").strip()
# Optional, https://developers.faceit.com — FACEIT nickname / level / region line
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
# Optional, lzt.market -> API token with the "market" scope: inventory value in LZT prices
LZT_API_TOKEN = os.getenv("LZT_API_TOKEN", "").strip()
LZT_API_URL = os.getenv("LZT_API_URL", "https://api.lzt.market").rstrip("/")
PRICES_PATH = BASE / "data" / "prices.json"

STATE_PATH = BASE / "data" / "state.json"
BANNERS_DIR = BASE / "data" / "banners"
DEFAULT_BANNER = BASE / "assets" / "tickets.png"

# Ticket games: key -> display name and default button emoji
GAMES = {
    "cs2": {"name": "CS2", "emoji": "🔫"},
    "dota2": {"name": "DOTA2", "emoji": "🛡️"},
    "rust": {"name": "RUST", "emoji": "🪓"},
    "tf2": {"name": "TF2", "emoji": "🎩"},
}
