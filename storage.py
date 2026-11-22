import json
from pathlib import Path

from config import GAMES, STATE_PATH


def default_settings() -> dict:
    return {
        "tickets_channel": None,      # channel with the ticket panel
        "tickets_category": None,     # category where a channel is created for every ticket
        "ticket_logs_channel": None,  # ticket cards + transcripts of closed tickets
        "member_logs_channel": None,  # join / leave logs
        "checker_channel": None,      # Steam inventory checker
        "checker_source": "lzt",      # inventory prices: lzt | steam | both (lzt falls back to steam)
        "checker_hint": "Пришлите ссылку на Steam профиль или SteamID64 — например `https://steamcommunity.com/profiles/7656…`",
        "auto_check": True,           # check the author's inventory right in a new ticket
        "reminder_enabled": False,    # ping while a ticket is not taken
        "reminder_minutes": 30,
        "reminder_role": None,        # None = speakers role of the ticket's game
        "reminder_max": 0,            # 0 = no limit
        "log_joins": True,
        "log_leaves": True,
        "staff_roles": [],
        "banner_url": "",
        "banner_file": None,          # uploaded file name in data/banners
        # role: speakers of this game, pinged in new tickets and allowed to take them
        "games": {key: {"enabled": True, "emoji": "", "role": None} for key in GAMES},
        # leaderboard settings
        "speakers_leaderboard_channel": None,  # channel for speakers leaderboard panel
        "workers_leaderboard_channel": None,   # channel for workers leaderboard panel
        "speaker_role": None,                  # role for speakers leaderboard filter
        "worker_role": None,                   # role for workers leaderboard filter
    }


class Store:
    """Everything the bot must remember across restarts, in one JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {
            "guilds": {},          # guild id -> settings
            "panels": {},          # key -> {"channel": id, "message": id}
            "tickets": {},         # ticket id -> ticket dict
            "next_ticket": 1,
            "join_channels": [],   # "join to create" voice channel ids
            "temp_channels": {},   # created voice channel id -> owner / state
            "daily_stats": {},     # guild id -> date -> {speakers, workers}
            "steam_tracking": {},  # user id -> [{steam64, nickname, last_status, added_at}]
        }
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def settings(self, guild_id: int) -> dict:
        """Guild settings with defaults filled in (new options appear automatically)."""
        current = self.data["guilds"].setdefault(str(guild_id), {})
        merged = default_settings()
        merged.update(current)
        merged["games"] = {key: {**default_settings()["games"][key], **current.get("games", {}).get(key, {})}
                           for key in GAMES}
        self.data["guilds"][str(guild_id)] = merged
        return merged

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


store = Store(STATE_PATH)
