import logging
from datetime import datetime

import discord
from discord.ext import commands

from storage import store

log = logging.getLogger("vex.member_logs")


def event_view(title: str, member: discord.Member, action: str, accent: int) -> discord.ui.LayoutView:
    # Local time of the machine the bot runs on, e.g. 12.11.2026 19:30:43
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    container = discord.ui.Container(accent_color=accent)
    container.add_item(discord.ui.TextDisplay(f"### {title}"))
    container.add_item(discord.ui.TextDisplay(
        f"{member.mention} ({member.name}) {action}.\n"
        f"Всего пользователей: {member.guild.member_count}"
    ))
    container.add_item(discord.ui.TextDisplay(f"-# **Дата и время: {now}**"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


class MemberLogs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def post(self, guild: discord.Guild, kind: str, view: discord.ui.LayoutView):
        settings = store.settings(guild.id)
        if not settings[kind]:
            return
        channel = guild.get_channel(settings["member_logs_channel"] or 0)
        if channel is None:
            return
        try:
            await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Cannot post member log in %s: %s", guild.id, e)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.post(member.guild, "log_joins", event_view(
            "Пользователь присоединился к серверу", member, "присоединился к серверу", 0x23A55A))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self.post(member.guild, "log_leaves", event_view(
            "Пользователь покинул сервер", member, "покинул сервер", 0xDA373C))


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberLogs(bot))
