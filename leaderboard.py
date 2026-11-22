"""Daily leaderboards: speakers and workers statistics.

Speakers: daily tickets taken count
Workers: daily tickets created count + daily inventory checks count
"""
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from storage import store

log = logging.getLogger("vex.leaderboard")

NO_PINGS = discord.AllowedMentions.none()


def today_key() -> str:
    """YYYY-MM-DD UTC for today."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_daily_stats(guild_id: int) -> dict:
    """Get or create today's stats for a guild."""
    guild_key = str(guild_id)
    if "daily_stats" not in store.data:
        store.data["daily_stats"] = {}
    
    today = today_key()
    if guild_key not in store.data["daily_stats"]:
        store.data["daily_stats"][guild_key] = {}
    
    guild_stats = store.data["daily_stats"][guild_key]
    if today not in guild_stats:
        guild_stats[today] = {
            "speakers": {},  # user_id -> tickets_taken_count
            "workers": {     # user_id -> {tickets_created, checks_done}
                "tickets": {},
                "checks": {}
            }
        }
    
    return guild_stats[today]


def reset_old_stats(guild_id: int) -> None:
    """Remove stats older than today."""
    guild_key = str(guild_id)
    if "daily_stats" not in store.data or guild_key not in store.data["daily_stats"]:
        return
    
    today = today_key()
    guild_stats = store.data["daily_stats"][guild_key]
    
    # Keep only today's stats
    old_days = [day for day in guild_stats.keys() if day != today]
    for day in old_days:
        del guild_stats[day]


def track_speaker_took_ticket(guild_id: int, speaker_id: int) -> None:
    """Increment speaker's daily ticket counter."""
    stats = get_daily_stats(guild_id)
    speaker_key = str(speaker_id)
    stats["speakers"][speaker_key] = stats["speakers"].get(speaker_key, 0) + 1
    store.save()


def track_worker_created_ticket(guild_id: int, user_id: int) -> None:
    """Increment worker's daily ticket creation counter."""
    stats = get_daily_stats(guild_id)
    user_key = str(user_id)
    stats["workers"]["tickets"][user_key] = stats["workers"]["tickets"].get(user_key, 0) + 1
    store.save()


def track_worker_inventory_check(guild_id: int, user_id: int) -> None:
    """Increment worker's daily inventory check counter."""
    stats = get_daily_stats(guild_id)
    user_key = str(user_id)
    stats["workers"]["checks"][user_key] = stats["workers"]["checks"].get(user_key, 0) + 1
    store.save()


def leaderboard_text(guild: discord.Guild, speaker_role_id: int | None, worker_role_id: int | None) -> tuple[str, str]:
    """Generate leaderboard text for speakers and workers.
    
    Returns: (speakers_text, workers_text)
    """
    stats = get_daily_stats(guild.id)
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    
    # Speakers leaderboard
    speakers_lines = [f"# 📊 Лидерборд спикеров\n**Дата:** {today}\n"]
    if speaker_role_id:
        speakers_lines.append(f"**Роль:** <@&{speaker_role_id}>\n")
    
    speaker_entries = []
    for speaker_id_str, count in stats["speakers"].items():
        speaker_id = int(speaker_id_str)
        member = guild.get_member(speaker_id)
        if member and (not speaker_role_id or any(r.id == speaker_role_id for r in member.roles)):
            speaker_entries.append((member, count))
    
    if speaker_entries:
        speaker_entries.sort(key=lambda x: x[1], reverse=True)
        speakers_lines.append("**Взято тикетов сегодня:**")
        for rank, (member, count) in enumerate(speaker_entries, 1):
            medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"**{rank}.**"
            speakers_lines.append(f"{medal} {member.mention} — **{count}** тикетов")
    else:
        speakers_lines.append("-# Пока нет активности за сегодня")
    
    speakers_text = "\n".join(speakers_lines)
    
    # Workers leaderboard
    workers_lines = [f"# 📊 Лидерборд воркеров\n**Дата:** {today}\n"]
    if worker_role_id:
        workers_lines.append(f"**Роль:** <@&{worker_role_id}>\n")
    
    worker_entries = []
    all_workers = set(stats["workers"]["tickets"].keys()) | set(stats["workers"]["checks"].keys())
    
    for worker_id_str in all_workers:
        worker_id = int(worker_id_str)
        member = guild.get_member(worker_id)
        if member and (not worker_role_id or any(r.id == worker_role_id for r in member.roles)):
            tickets = stats["workers"]["tickets"].get(worker_id_str, 0)
            checks = stats["workers"]["checks"].get(worker_id_str, 0)
            total = tickets + checks
            worker_entries.append((member, tickets, checks, total))
    
    if worker_entries:
        worker_entries.sort(key=lambda x: x[3], reverse=True)
        workers_lines.append("**Активность сегодня:**")
        for rank, (member, tickets, checks, total) in enumerate(worker_entries, 1):
            medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"**{rank}.**"
            parts = []
            if tickets > 0:
                parts.append(f"🎫 {tickets}")
            if checks > 0:
                parts.append(f"🔍 {checks}")
            activity = " | ".join(parts) if parts else "—"
            workers_lines.append(f"{medal} {member.mention} — {activity} (всего: **{total}**)")
    else:
        workers_lines.append("-# Пока нет активности за сегодня")
    
    workers_text = "\n".join(workers_lines)
    
    return speakers_text, workers_text


def speakers_panel_view(guild: discord.Guild, speaker_role_id: int | None) -> discord.ui.LayoutView:
    """Panel view for speakers leaderboard."""
    speakers_text, _ = leaderboard_text(guild, speaker_role_id, None)
    
    container = discord.ui.Container(accent_color=0x5865F2)
    container.add_item(discord.ui.TextDisplay(speakers_text))
    container.add_item(discord.ui.TextDisplay("-# Обновляется автоматически. Статистика сбрасывается в 00:00 UTC."))
    
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def workers_panel_view(guild: discord.Guild, worker_role_id: int | None) -> discord.ui.LayoutView:
    """Panel view for workers leaderboard."""
    _, workers_text = leaderboard_text(guild, None, worker_role_id)
    
    container = discord.ui.Container(accent_color=0xF0B232)
    container.add_item(discord.ui.TextDisplay(workers_text))
    container.add_item(discord.ui.TextDisplay(
        "-# 🎫 = тикеты созданы, 🔍 = инвентарь проверен\n"
        "-# Обновляется автоматически. Статистика сбрасывается в 00:00 UTC."
    ))
    
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


async def refresh_speakers_panel(bot: commands.Bot, guild_id: int) -> None:
    """Update the speakers leaderboard panel message."""
    settings = store.settings(guild_id)
    if not settings.get("speakers_leaderboard_channel"):
        return
    
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    
    speaker_role = settings.get("speaker_role")
    
    async def send(channel: discord.abc.Messageable) -> discord.Message:
        return await channel.send(view=speakers_panel_view(guild, speaker_role))
    
    await bot.ensure_panel(
        f"speakers_leaderboard:{guild_id}",
        settings["speakers_leaderboard_channel"],
        send,
        force=True
    )


async def refresh_workers_panel(bot: commands.Bot, guild_id: int) -> None:
    """Update the workers leaderboard panel message."""
    settings = store.settings(guild_id)
    if not settings.get("workers_leaderboard_channel"):
        return
    
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    
    worker_role = settings.get("worker_role")
    
    async def send(channel: discord.abc.Messageable) -> discord.Message:
        return await channel.send(view=workers_panel_view(guild, worker_role))
    
    await bot.ensure_panel(
        f"workers_leaderboard:{guild_id}",
        settings["workers_leaderboard_channel"],
        send,
        force=True
    )


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.daily_reset.start()
    
    async def cog_unload(self):
        self.daily_reset.cancel()
    
    @tasks.loop(minutes=1)
    async def daily_reset(self):
        """Check if it's midnight UTC and reset statistics."""
        now = datetime.now(timezone.utc)
        
        # Reset at 00:00 UTC
        if now.hour == 0 and now.minute == 0:
            log.info("Daily reset triggered at midnight UTC")
            for guild in self.bot.guilds:
                reset_old_stats(guild.id)
                await refresh_speakers_panel(self.bot, guild.id)
                await refresh_workers_panel(self.bot, guild.id)
            store.save()
    
    @daily_reset.before_loop
    async def before_daily_reset(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboard(bot))
