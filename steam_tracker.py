"""Steam online tracker: notify users when tracked Steam profiles come online."""
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from storage import store

log = logging.getLogger("vex.steam_tracker")

STEAM_API_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
CHECK_INTERVAL_MINUTES = 5  # How often to check Steam API


def get_tracked_profiles(user_id: int) -> list[dict]:
    """Get all Steam profiles tracked by a Discord user."""
    if "steam_tracking" not in store.data:
        store.data["steam_tracking"] = {}
    
    user_key = str(user_id)
    return store.data["steam_tracking"].get(user_key, [])


def add_tracked_profile(user_id: int, steam64: str, nickname: str = None) -> bool:
    """Add a Steam profile to track. Returns True if added, False if already tracked."""
    if "steam_tracking" not in store.data:
        store.data["steam_tracking"] = {}
    
    user_key = str(user_id)
    profiles = store.data["steam_tracking"].get(user_key, [])
    
    # Check if already tracking
    if any(p["steam64"] == steam64 for p in profiles):
        return False
    
    profiles.append({
        "steam64": steam64,
        "nickname": nickname or steam64,
        "last_status": None,  # 0 = offline, 1 = online
        "added_at": int(discord.utils.utcnow().timestamp())
    })
    
    store.data["steam_tracking"][user_key] = profiles
    store.save()
    return True


def remove_tracked_profile(user_id: int, steam64: str) -> bool:
    """Remove a Steam profile from tracking. Returns True if removed, False if not found."""
    if "steam_tracking" not in store.data:
        return False
    
    user_key = str(user_id)
    profiles = store.data["steam_tracking"].get(user_key, [])
    
    original_count = len(profiles)
    profiles = [p for p in profiles if p["steam64"] != steam64]
    
    if len(profiles) < original_count:
        store.data["steam_tracking"][user_key] = profiles
        store.save()
        return True
    
    return False


async def check_steam_status(steam64: str) -> int | None:
    """Check if Steam profile is online. Returns: 1 = online, 0 = offline, None = error."""
    if not config.STEAM_API_KEY:
        return None
    
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            params = {"key": config.STEAM_API_KEY, "steamids": steam64}
            async with session.get(STEAM_API_URL, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                players = data.get("response", {}).get("players", [])
                if not players:
                    return None
                
                # personastate: 0 = Offline, 1 = Online, 2 = Busy, 3 = Away, etc.
                state = players[0].get("personastate", 0)
                return 1 if state > 0 else 0
    except Exception as e:
        log.warning("Failed to check Steam status for %s: %s", steam64, e)
        return None


def note_view(text: str, color: int = 0xF0B232) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=color))
    return view


class SteamTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.checker_task.start()
    
    async def cog_unload(self):
        self.checker_task.cancel()
    
    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def checker_task(self):
        """Periodically check all tracked Steam profiles."""
        if not config.STEAM_API_KEY:
            return
        
        if "steam_tracking" not in store.data or not store.data["steam_tracking"]:
            return
        
        log.info("Checking tracked Steam profiles...")
        
        for user_id_str, profiles in store.data["steam_tracking"].items():
            user_id = int(user_id_str)
            user = self.bot.get_user(user_id)
            if not user:
                try:
                    user = await self.bot.fetch_user(user_id)
                except discord.NotFound:
                    continue
            
            for profile in profiles:
                steam64 = profile["steam64"]
                current_status = await check_steam_status(steam64)
                
                if current_status is None:
                    continue
                
                last_status = profile.get("last_status")
                
                # Notify only when status changes from offline to online
                if last_status == 0 and current_status == 1:
                    nickname = profile.get("nickname", steam64)
                    await self.send_notification(user, steam64, nickname)
                    log.info("Notified %s about %s coming online", user_id, steam64)
                
                # Update status
                profile["last_status"] = current_status
        
        store.save()
    
    @checker_task.before_loop
    async def before_checker(self):
        await self.bot.wait_until_ready()
    
    async def send_notification(self, user: discord.User, steam64: str, nickname: str):
        """Send DM notification that a tracked profile came online."""
        try:
            view = note_view(
                f"🟢 **{discord.utils.escape_markdown(nickname)}** зашёл в сеть!\n\n"
                f"**Steam ID64:** `{steam64}`\n"
                f"**Профиль:** https://steamcommunity.com/profiles/{steam64}\n"
                f"-# Уведомление от Steam трекера",
                color=0x23A55A
            )
            await user.send(view=view)
        except discord.Forbidden:
            log.warning("Cannot send DM to user %s", user.id)
        except Exception as e:
            log.error("Failed to notify user %s: %s", user.id, e)
    
    @app_commands.command(name="privyaz", description="Отслеживать Steam профиль и получать уведомления когда он заходит в сеть")
    @app_commands.describe(steamid="Steam ID64 или ссылка на профиль")
    async def privyaz(self, interaction: discord.Interaction, steamid: str):
        """Track a Steam profile and get notified when they come online."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if not config.STEAM_API_KEY:
            return await interaction.followup.send(
                view=note_view("⚠️ Steam API ключ не настроен. Обратитесь к администратору бота."),
                ephemeral=True
            )
        
        # Try to resolve Steam ID
        from checker import Checker
        checker = self.bot.get_cog("Checker")
        if not checker:
            return await interaction.followup.send(
                view=note_view("⚠️ Модуль чекера не загружен."),
                ephemeral=True
            )
        
        steam64 = await checker.steam.resolve(steamid.strip())
        if not steam64:
            return await interaction.followup.send(
                view=note_view("❌ Не удалось определить Steam ID. Проверьте ссылку или ID."),
                ephemeral=True
            )
        
        # Get nickname from Steam
        nickname = None
        try:
            summary = await checker.steam.summary(steam64)
            if summary:
                nickname = summary.get("personaname", steam64)
        except Exception:
            nickname = steam64
        
        # Add to tracking
        added = add_tracked_profile(interaction.user.id, steam64, nickname)
        
        if not added:
            return await interaction.followup.send(
                view=note_view(f"⚠️ Вы уже отслеживаете профиль **{discord.utils.escape_markdown(nickname)}**"),
                ephemeral=True
            )
        
        # Check initial status
        initial_status = await check_steam_status(steam64)
        status_text = "🟢 онлайн" if initial_status == 1 else "⚪ оффлайн" if initial_status == 0 else "❓ неизвестно"
        
        await interaction.followup.send(
            view=note_view(
                f"✅ Теперь отслеживается профиль **{discord.utils.escape_markdown(nickname)}**\n\n"
                f"**Steam ID64:** `{steam64}`\n"
                f"**Статус:** {status_text}\n\n"
                f"Вы получите уведомление в ЛС, когда этот человек зайдёт в сеть.\n"
                f"-# Проверка каждые {CHECK_INTERVAL_MINUTES} минут",
                color=0x23A55A
            ),
            ephemeral=True
        )
    
    @app_commands.command(name="otslezhivaemye", description="Показать список отслеживаемых Steam профилей")
    async def otslezhivaemye(self, interaction: discord.Interaction):
        """Show list of tracked Steam profiles."""
        await interaction.response.defer(ephemeral=True)
        
        profiles = get_tracked_profiles(interaction.user.id)
        
        if not profiles:
            return await interaction.followup.send(
                view=note_view("📋 Вы пока не отслеживаете ни одного профиля.\n\n"
                              "Используйте `/privyaz <steamid>` чтобы начать отслеживать."),
                ephemeral=True
            )
        
        lines = [f"📋 **Отслеживаемые профили ({len(profiles)}):**\n"]
        
        for i, profile in enumerate(profiles, 1):
            nickname = profile.get("nickname", profile["steam64"])
            steam64 = profile["steam64"]
            status = profile.get("last_status")
            
            if status == 1:
                status_icon = "🟢 онлайн"
            elif status == 0:
                status_icon = "⚪ оффлайн"
            else:
                status_icon = "❓ проверяется..."
            
            lines.append(
                f"{i}. **{discord.utils.escape_markdown(nickname)}**\n"
                f"   Steam ID: `{steam64}`\n"
                f"   Статус: {status_icon}"
            )
        
        lines.append(f"\n-# Проверка каждые {CHECK_INTERVAL_MINUTES} минут")
        lines.append(f"-# Используйте `/otprivyaz <steamid>` чтобы удалить профиль")
        
        await interaction.followup.send(
            view=note_view("\n".join(lines)),
            ephemeral=True
        )
    
    @app_commands.command(name="otprivyaz", description="Перестать отслеживать Steam профиль")
    @app_commands.describe(steamid="Steam ID64 профиля")
    async def otprivyaz(self, interaction: discord.Interaction, steamid: str):
        """Stop tracking a Steam profile."""
        await interaction.response.defer(ephemeral=True)
        
        steam64 = steamid.strip()
        
        # Try to extract Steam64 from URL if needed
        import re
        match = re.search(r"7656119\d{10}", steam64)
        if match:
            steam64 = match.group(0)
        
        removed = remove_tracked_profile(interaction.user.id, steam64)
        
        if removed:
            await interaction.followup.send(
                view=note_view(
                    f"✅ Профиль `{steam64}` больше не отслеживается.",
                    color=0x23A55A
                ),
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                view=note_view(
                    f"❌ Профиль `{steam64}` не найден в вашем списке отслеживания.\n\n"
                    f"Используйте `/otslezhivaemye` чтобы посмотреть список.",
                ),
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamTracker(bot))
