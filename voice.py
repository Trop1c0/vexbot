import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from storage import store

log = logging.getLogger("vex.voice")

NO_PINGS = discord.AllowedMentions.none()


def temp_record(channel_id: int) -> dict | None:
    return store.data["temp_channels"].get(str(channel_id))


def owned_channel(guild: discord.Guild, member_id: int) -> discord.VoiceChannel | None:
    for cid, rec in store.data["temp_channels"].items():
        if rec["owner"] == member_id:
            channel = guild.get_channel(int(cid))
            if isinstance(channel, discord.VoiceChannel):
                return channel
    return None


def note_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text)))
    return view


# ---------- management panel ----------

def panel_rows(rec: dict) -> list[tuple[str, str, str, str]]:
    """(description, action, button label, emoji)"""
    return [
        ("Change the member limit", "slots", "Change slots", "🔢"),
        ("Rename the channel", "rename", "Rename channel", "✏️"),
        ("Grant access to selected users", "grant", "Grant user access", "➕"),
        ("Remove access from selected users", "remove", "Remove user access", "➖"),
        ("Transfer channel ownership", "transfer", "Transfer crown", "👑"),
        ("Allow others to join" if rec["locked"] else "Stop others from joining",
         "lock", "Unlock channel" if rec["locked"] else "Lock channel", "🔓" if rec["locked"] else "🔒"),
        ("Show the channel to others" if rec["hidden"] else "Hide the channel from others",
         "hide", "Show channel" if rec["hidden"] else "Hide channel", "👁️" if rec["hidden"] else "🙈"),
    ]


def panel_view(rec: dict) -> discord.ui.LayoutView:
    container = discord.ui.Container()
    container.add_item(discord.ui.TextDisplay(f"## Management:\n-# Channel owner(s): <@{rec['owner']}>"))
    for text, action, label, emoji in panel_rows(rec):
        container.add_item(discord.ui.Section(text, accessory=VoiceButton(action, label, emoji)))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


async def refresh_panel(channel: discord.VoiceChannel, rec: dict) -> None:
    if not rec.get("panel"):
        return
    try:
        await channel.get_partial_message(rec["panel"]).edit(view=panel_view(rec), allowed_mentions=NO_PINGS)
    except discord.HTTPException as e:
        log.warning("Cannot refresh panel in %s: %s", channel.id, e)


async def set_everyone(channel: discord.VoiceChannel, rec: dict, **perms) -> None:
    overwrite = channel.overwrites_for(channel.guild.default_role)
    for name, value in perms.items():
        setattr(overwrite, name, value)
    await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)


class VoiceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"vc:(?P<action>[a-z]+)"):
    def __init__(self, action: str, label: str | None = None, emoji: str | None = None):
        self.action = action
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"vc:{action}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], item.label, item.emoji)

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.channel
        rec = temp_record(interaction.channel_id)
        if rec is None or not isinstance(channel, discord.VoiceChannel):
            return await interaction.response.send_message(view=note_view("Этот канал больше не управляется ботом."), ephemeral=True)
        member = interaction.user
        if member.id != rec["owner"] and not member.guild_permissions.manage_channels:
            return await interaction.response.send_message(view=note_view("Только владелец канала может это делать."), ephemeral=True)

        if self.action == "slots":
            await interaction.response.send_modal(SlotsModal(channel))
        elif self.action == "rename":
            await interaction.response.send_modal(RenameModal(channel))
        elif self.action in PICKERS:
            await interaction.response.send_message(view=PickView(channel, self.action), ephemeral=True)
        elif self.action == "lock":
            rec["locked"] = not rec["locked"]
            await set_everyone(channel, rec, connect=False if rec["locked"] else rec["base"]["connect"])
            store.save()
            await interaction.response.edit_message(view=panel_view(rec), allowed_mentions=NO_PINGS)
        elif self.action == "hide":
            rec["hidden"] = not rec["hidden"]
            await set_everyone(channel, rec, view_channel=False if rec["hidden"] else rec["base"]["view_channel"])
            store.save()
            await interaction.response.edit_message(view=panel_view(rec), allowed_mentions=NO_PINGS)


class SlotsModal(discord.ui.Modal, title="Change slots"):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel
        self.value = discord.ui.TextInput(
            placeholder="0 — без лимита, максимум 99", default=str(channel.user_limit), max_length=2, required=True,
        )
        self.add_item(discord.ui.Label(text="Лимит участников", component=self.value))

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.value.value.strip()
        if not raw.isdigit() or not 0 <= int(raw) <= 99:
            return await interaction.response.send_message(view=note_view("Введите число от 0 до 99."), ephemeral=True)
        await self.channel.edit(user_limit=int(raw))
        text = "Лимит снят." if raw == "0" else f"Лимит: **{int(raw)}**."
        await interaction.response.send_message(view=note_view(text), ephemeral=True)


class RenameModal(discord.ui.Modal, title="Rename channel"):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel
        self.value = discord.ui.TextInput(default=channel.name, min_length=1, max_length=100, required=True)
        self.add_item(discord.ui.Label(text="Новое название", component=self.value))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # Discord allows 2 renames per 10 minutes; don't wait out a long rate limit
            await asyncio.wait_for(self.channel.edit(name=self.value.value.strip()), timeout=8)
        except asyncio.TimeoutError:
            return await interaction.followup.send(
                view=note_view("Discord разрешает переименовывать канал 2 раза в 10 минут. Попробуйте позже."), ephemeral=True)
        await interaction.followup.send(view=note_view(f"Канал переименован в **{self.channel.name}**."), ephemeral=True)


# ---------- user pickers ----------

PICKERS = {
    "grant": ("Кому выдать доступ к каналу?", 10),
    "remove": ("У кого забрать доступ к каналу?", 10),
    "transfer": ("Кому передать владение каналом?", 1),
}


class PickView(discord.ui.LayoutView):
    def __init__(self, channel: discord.VoiceChannel, action: str):
        super().__init__(timeout=120)
        self.channel = channel
        self.action = action
        prompt, max_values = PICKERS[action]
        select = discord.ui.UserSelect(placeholder="Выберите пользователей", min_values=1, max_values=max_values)
        select.callback = self.picked
        self.select = select
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(prompt), discord.ui.ActionRow(select)))

    async def picked(self, interaction: discord.Interaction):
        rec = temp_record(self.channel.id)
        if rec is None:
            return await interaction.response.edit_message(view=note_view("Канал уже удалён."))
        if interaction.user.id != rec["owner"] and not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.edit_message(view=note_view("Вы больше не владелец канала."))
        users = [u for u in self.select.values if isinstance(u, discord.Member) and not u.bot]
        if not users:
            return await interaction.response.edit_message(view=note_view("Нужно выбрать участников сервера (не ботов)."))
        await interaction.response.defer()

        names = ", ".join(u.mention for u in users)
        if self.action == "grant":
            for user in users:
                await self.channel.set_permissions(user, view_channel=True, connect=True)
            text = f"Доступ выдан: {names}"
        elif self.action == "remove":
            users = [u for u in users if u.id != rec["owner"]]
            for user in users:
                await self.channel.set_permissions(user, view_channel=False if rec["hidden"] else None, connect=False)
                if user.voice and user.voice.channel == self.channel:
                    await user.move_to(None)
            text = f"Доступ забран: {', '.join(u.mention for u in users)}" if users else "Нельзя забрать доступ у владельца."
        else:
            new_owner = users[0]
            rec["owner"] = new_owner.id
            store.save()
            await self.channel.set_permissions(new_owner, view_channel=True, connect=True, speak=True)
            await refresh_panel(self.channel, rec)
            text = f"Владелец канала теперь {new_owner.mention}"
        await interaction.edit_original_response(view=note_view(text), allowed_mentions=NO_PINGS)


# ---------- cog ----------

async def create_join_channel(category: discord.CategoryChannel, name: str, reason: str) -> discord.VoiceChannel:
    channel = await category.create_voice_channel(name=(name.strip() or "➕ Join to create")[:100], reason=reason)
    store.data["join_channels"].append(channel.id)
    store.save()
    return channel


class Voice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.creating: set[int] = set()

    @app_commands.command(name="createjoinchannel", description="Создать канал Join to Create")
    @app_commands.describe(category="Категория, где будут создаваться каналы", name="Название канала")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def createjoinchannel(self, interaction: discord.Interaction, category: discord.CategoryChannel,
                                name: str = "➕ Join to create"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await create_join_channel(category, name, f"Join to create by {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(view=note_view("У бота нет прав создавать каналы в этой категории."), ephemeral=True)
        await interaction.followup.send(view=note_view(
            f"✅ Создан канал {channel.mention} в категории **{category.name}**.\n"
            "-# Зайдите в него — бот создаст личный голосовой канал и отправит в его чат панель управления."
        ), ephemeral=True)

    @commands.Cog.listener()
    async def on_vex_startup(self):
        # Clean up whatever changed while the bot was offline
        store.data["join_channels"] = [cid for cid in store.data["join_channels"] if self.bot.get_channel(cid)]
        for cid in list(store.data["temp_channels"]):
            channel = self.bot.get_channel(int(cid))
            if channel is None:
                store.data["temp_channels"].pop(cid)
            elif not channel.members:
                await self.delete_temp(channel)
        store.save()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        changed = False
        if channel.id in store.data["join_channels"]:
            store.data["join_channels"].remove(channel.id)
            changed = True
        if store.data["temp_channels"].pop(str(channel.id), None):
            changed = True
        if changed:
            store.save()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel == after.channel:
            return
        if after.channel and after.channel.id in store.data["join_channels"]:
            await self.join_to_create(member, after.channel)
        if before.channel and temp_record(before.channel.id) and not before.channel.members:
            await self.delete_temp(before.channel)

    async def join_to_create(self, member: discord.Member, hub: discord.VoiceChannel):
        if member.id in self.creating:
            return
        existing = owned_channel(member.guild, member.id)
        if existing:
            try:
                await member.move_to(existing)
            except discord.HTTPException:
                pass
            return

        self.creating.add(member.id)
        try:
            category = hub.category
            overwrites = dict(category.overwrites) if category else {}
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
            me = member.guild.me
            overwrites[me] = discord.PermissionOverwrite(view_channel=True, connect=True, send_messages=True)
            channel = await member.guild.create_voice_channel(
                name=f"{member.display_name}'s channel"[:100], category=category, overwrites=overwrites,
                user_limit=hub.user_limit, bitrate=hub.bitrate, reason=f"Join to create: {member}",
            )
            everyone = channel.overwrites_for(member.guild.default_role)
            rec = {
                "owner": member.id, "locked": False, "hidden": False, "panel": None,
                "base": {"connect": everyone.connect, "view_channel": everyone.view_channel},
            }
            store.data["temp_channels"][str(channel.id)] = rec
            store.save()
            try:
                await member.move_to(channel)
            except discord.HTTPException:
                await self.delete_temp(channel)  # user already left the hub
                return
            panel = await channel.send(view=panel_view(rec), allowed_mentions=NO_PINGS)
            rec["panel"] = panel.id
            store.save()
        except discord.HTTPException as e:
            log.error("Join to create failed for %s: %s", member, e)
        finally:
            self.creating.discard(member.id)

    async def delete_temp(self, channel: discord.abc.GuildChannel):
        store.data["temp_channels"].pop(str(channel.id), None)
        store.save()
        try:
            await channel.delete(reason="Join to create: channel is empty")
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            log.warning("Cannot delete temp channel %s: %s", channel.id, e)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(VoiceButton)
    await bot.add_cog(Voice(bot))
