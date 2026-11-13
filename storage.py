import json
from pathlib import Path

from config import GAMES, STATE_PATH


def default_settings() -> dict:
    return {
        "tickets_channel": None,      # channel with the ticket panel
        "tickets_category": None,     # category where a channel is created for every ticket
        "ticket_logs_channel": None,  # ticket cards + transcripts of closed tickets
        "member_logs_channel": None,  # join / leave logs
        "log_joins": True,
        "log_leaves": True,
        "staff_roles": [],
        "banner_url": "",
        "banner_file": None,          # uploaded file name in data/banners
        # role: speakers of this game, pinged in new tickets and allowed to take them
        "games": {key: {"enabled": True, "emoji": "", "role": None} for key in GAMES},
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
