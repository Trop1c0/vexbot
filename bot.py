import logging
from typing import Awaitable, Callable

import discord
from discord.ext import commands

import config
from storage import store

log = logging.getLogger("vex")

EXTENSIONS = ("tickets", "voice", "member_logs", "checker", "api", "leaderboard")


class VexBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # join / leave logs, dashboard permission checks
        intents.message_content = True  # relaying ticket messages
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self._startup_done = False

    async def setup_hook(self):
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        if config.DEV_GUILD_ID:
            guild = discord.Object(config.DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (%s) on %d servers", self.user, self.user.id, len(self.guilds))
        if self._startup_done:  # on_ready fires again after reconnects
            return
        self._startup_done = True
        self.dispatch("vex_startup")

    async def ensure_panel(self, key: str, channel_id: int | None,
                           send: Callable[[discord.abc.Messageable], Awaitable[discord.Message]],
                           force: bool = False) -> discord.Message | None:
        """Posts a panel once. Not reposted unless it was deleted, the channel changed or force=True.

        Raises discord.HTTPException when the channel can't be used, so the dashboard can show why."""
        record = store.data["panels"].get(key)
        if not channel_id:
            return None
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)

        if record and record["channel"] == channel_id and not force:
            try:
                return await channel.fetch_message(record["message"])
            except discord.NotFound:
                log.info("Panel %s was deleted, posting again", key)

        if record:  # remove the old copy (other channel or forced refresh)
            try:
                old = self.get_partial_messageable(record["channel"])
                await old.get_partial_message(record["message"]).delete()
            except discord.HTTPException:
                pass

        message = await send(channel)
        store.data["panels"][key] = {"channel": channel_id, "message": message.id}
        store.save()
        log.info("Panel %s posted in #%s", key, channel)
        return message


bot = VexBot()


if __name__ == "__main__":
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")
    discord.utils.setup_logging(level=logging.INFO)
    bot.run(config.TOKEN, log_handler=None)
