import asyncio
import io
import logging
import re

import aiohttp
import discord
from discord.ext import commands

import config
from config import GAMES
from storage import store

log = logging.getLogger("vex.tickets")

NO_PINGS = discord.AllowedMentions.none()

# status -> (emoji, label, container accent)
STATUS = {
    "pending": ("🕓", "Ожидает спикера", 0xF0B232),
    "accepted": ("✅", "В работе", 0x23A55A),
    "closed": ("🔒", "Завершён", 0x80848E),
    # statuses of tickets created by older versions
    "declined": ("⛔", "Отклонен", 0xDA373C),
    "cancelled": ("❌", "Отменен", 0x80848E),
}
OPEN_STATUSES = {"pending", "accepted"}
DELETE_DELAY = 10          # seconds between "Завершить тикет" and deleting the channel
TRANSCRIPT_LIMIT = 3000    # messages saved to the transcript file
STEAM64_BASE = 76561197960265728


# ---------- helpers ----------

def open_ticket_of(user_id: int, guild_id: int) -> dict | None:
    for ticket in store.data["tickets"].values():
        if ticket["user"] == user_id and ticket.get("guild") == guild_id and ticket["status"] in OPEN_STATUSES:
            return ticket
    return None


def ticket_by_channel(channel_id: int) -> dict | None:
    for ticket in store.data["tickets"].values():
        if ticket.get("channel") == channel_id:
            return ticket
    return None


def game_role(ticket: dict) -> int | None:
    return store.settings(ticket["guild"])["games"].get(ticket["game"], {}).get("role")


def is_staff(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_messages:
        return True
    staff = store.settings(member.guild.id)["staff_roles"]
    return any(role.id in staff for role in member.roles)


def is_speaker(member: discord.abc.User, ticket: dict) -> bool:
    """Staff or a speaker of this ticket's game."""
    if is_staff(member):
        return True
    role = game_role(ticket)
    return isinstance(member, discord.Member) and role is not None and any(r.id == role for r in member.roles)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


def plural_tickets(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "тикет"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "тикета"
    return "тикетов"


def slug(text: str) -> str:
    text = re.sub(r"[^\w-]+", "-", text.lower(), flags=re.UNICODE).strip("-_")
    return re.sub(r"-{2,}", "-", text) or "ticket"


async def resolve_steam64(text: str) -> str | None:
    """SteamID64 from a profile link, SteamID64, STEAM_0:X:Y, [U:1:N] or a /id/<name> link."""
    text = text.strip()
    if m := re.search(r"(?<!\d)(7656119\d{10})(?!\d)", text):
        return m[1]
    if m := re.search(r"STEAM_[0-5]:([01]):(\d+)", text, re.I):
        return str(STEAM64_BASE + int(m[2]) * 2 + int(m[1]))
    if m := re.search(r"\[U:1:(\d+)\]", text, re.I):
        return str(STEAM64_BASE + int(m[1]))
    if m := re.search(r"steamcommunity\.com/id/([\w.-]+)", text, re.I):
        try:
            timeout = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(f"https://steamcommunity.com/id/{m[1]}/?xml=1") as resp:
                    found = re.search(r"<steamID64>(\d{17})</steamID64>", await resp.text())
                    return found[1] if found else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
    return None


def previous_tickets(ticket: dict) -> int:
    if not ticket.get("steam64"):
        return 0
    return sum(1 for t in store.data["tickets"].values()
               if t.get("steam64") == ticket["steam64"] and t.get("guild") == ticket["guild"] and t["id"] != ticket["id"])


# ---------- panel ----------

def panel_view(banner: str | None, settings: dict) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()
    if banner:
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(banner)))
        container.add_item(discord.ui.Separator())
    buttons = [OpenTicketButton(key, game["emoji"] or GAMES[key]["emoji"])
               for key, game in settings["games"].items() if game["enabled"]]
    if buttons:
        container.add_item(discord.ui.ActionRow(*buttons[:5]))
    else:
        container.add_item(discord.ui.TextDisplay("-# Тикеты сейчас закрыты."))
    view.add_item(container)
    return view


def banner_source(guild_id: int) -> tuple[str | None, discord.File | None]:
    """(media url for the view, file to attach) for the guild's ticket banner."""
    settings = store.settings(guild_id)
    path = None
    if settings["banner_file"]:
        path = config.BANNERS_DIR / settings["banner_file"]
    elif not settings["banner_url"] and config.DEFAULT_BANNER.exists():
        path = config.DEFAULT_BANNER
    if path and path.exists():
        name = "tickets" + path.suffix
        return f"attachment://{name}", discord.File(path, filename=name)
    return settings["banner_url"] or None, None


async def post_panel(client: commands.Bot, guild_id: int, force: bool = False) -> discord.Message | None:
    settings = store.settings(guild_id)

    async def send(channel: discord.abc.Messageable) -> discord.Message:
        url, file = banner_source(guild_id)
        if file:
            return await channel.send(view=panel_view(url, settings), file=file)
        return await channel.send(view=panel_view(url, settings))

    return await client.ensure_panel(f"tickets:{guild_id}", settings["tickets_channel"], send, force=force)


# ---------- ticket views ----------

def ticket_view(ticket: dict) -> discord.ui.LayoutView:
    """The first message in the ticket channel."""
    status = ticket["status"]
    container = discord.ui.Container(accent_color=STATUS[status][2])
    container.add_item(discord.ui.TextDisplay(f"Новый тикет от <@{ticket['user']}>"))

    if status == "pending":
        role = game_role(ticket)
        who = f"<@&{role}>" if role else "спикер"
        container.add_item(discord.ui.TextDisplay(f"Ожидайте, скоро {who} ответит в ближайшее время."))
    elif status == "accepted":
        container.add_item(discord.ui.TextDisplay(f"✅ Тикет взял спикер <@{ticket['speaker']}>"))
    else:
        by = f" <@{ticket['closed_by']}>" if ticket.get("closed_by") else ""
        container.add_item(discord.ui.TextDisplay(f"🔒 Тикет завершён{by}"))

    note = ticket["note"].replace("```", "ʼʼʼ")
    container.add_item(discord.ui.TextDisplay(f"▪️ **Информация для спикера:**\n```\n{note}\n```"))

    if ticket.get("steam64"):
        steam = (f"**Steam ID64:** `{ticket['steam64']}` · "
                 f"[профиль](https://steamcommunity.com/profiles/{ticket['steam64']})")
        count = ticket.get("previous", 0)
        verb = "найден" if plural_tickets(count) == "тикет" else "найдено"
        found = (f"🟥 С этим SteamID {verb} {count} {plural_tickets(count)}" if count
                 else "🟩 С этим SteamID тикетов ещё не было")
        container.add_item(discord.ui.TextDisplay(f"{steam}\n{found}"))
    else:
        container.add_item(discord.ui.TextDisplay(f"**Steam:** {ticket['steam']}\n-# SteamID64 определить не удалось"))

    if status in OPEN_STATUSES:
        container.add_item(discord.ui.Separator())
        buttons = [TakeTicketButton(ticket["id"])] if status == "pending" else []
        buttons.append(CloseTicketButton(ticket["id"]))
        container.add_item(discord.ui.ActionRow(*buttons))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def take_request_view(ticket: dict, speaker_id: int, agreed: bool = False) -> discord.ui.LayoutView:
    container = discord.ui.Container(accent_color=0x23A55A if agreed else None)
    if agreed:
        container.add_item(discord.ui.TextDisplay(
            f"✅ <@{ticket['user']}> согласился. Тикет взял спикер <@{speaker_id}>"))
    else:
        container.add_item(discord.ui.TextDisplay(
            f"<@{ticket['user']}> ваш тикет хочет взять спикер <@{speaker_id}>"))
        container.add_item(discord.ui.ActionRow(AgreeTakeButton(ticket["id"], speaker_id)))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def log_view(ticket: dict) -> discord.ui.LayoutView:
    """Card in the ticket logs channel."""
    game = GAMES[ticket["game"]]["name"]
    emoji, label, accent = STATUS[ticket["status"]]
    header = f"### {game} тикет #{ticket['id']} от <@{ticket['user']}>"
    container = discord.ui.Container(accent_color=accent)
    if ticket["status"] in OPEN_STATUSES and ticket.get("channel_url"):
        container.add_item(discord.ui.Section(header, accessory=discord.ui.Button(
            label="Открыть канал", url=ticket["channel_url"], style=discord.ButtonStyle.link)))
    else:
        container.add_item(discord.ui.TextDisplay(header))
    status = f"{emoji} Статус: **{label}**"
    if ticket.get("speaker"):
        status += f" · спикер <@{ticket['speaker']}>"
    if ticket["status"] == "closed" and ticket.get("closed_by"):
        status += f" · завершил <@{ticket['closed_by']}>"
    container.add_item(discord.ui.TextDisplay(quote(status)))
    container.add_item(discord.ui.Separator())
    steam = f"`{ticket['steam64']}`" if ticket.get("steam64") else ticket["steam"]
    container.add_item(discord.ui.TextDisplay(
        f"**Пользователь:** <@{ticket['user']}> (`{ticket['username']}`, `{ticket['user']}`)\n"
        f"**Steam:** {steam}\n"
        f"**Информация для спикера:**\n{quote(ticket['note'])}"
    ))
    container.add_item(discord.ui.TextDisplay(f"-# Создан <t:{ticket['created']}:f>"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def note_view(text: str, accent: int | None = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=accent))
    return view


# ---------- updates ----------

async def refresh_messages(client: discord.Client, ticket: dict) -> None:
    """Re-render the ticket's main message and its log card."""
    targets = [("channel", "message", ticket_view), ("log_channel", "log_message", log_view)]
    for channel_key, message_key, render in targets:
        if not ticket.get(channel_key) or not ticket.get(message_key):
            continue
        try:
            channel = client.get_partial_messageable(ticket[channel_key])
            await channel.get_partial_message(ticket[message_key]).edit(view=render(ticket), allowed_mentions=NO_PINGS)
        except discord.HTTPException as e:
            if channel_key == "log_channel" or ticket["status"] in OPEN_STATUSES:
                log.warning("Ticket %s: cannot update %s: %s", ticket["id"], message_key, e)


async def save_transcript(client: discord.Client, ticket: dict, channel: discord.TextChannel) -> None:
    logs = client.get_channel(ticket.get("log_channel") or 0)
    if not isinstance(logs, discord.TextChannel):
        return
    lines = [f"Тикет #{ticket['id']} · {GAMES[ticket['game']]['name']} · {ticket['username']} ({ticket['user']})",
             f"Steam: {ticket.get('steam64') or ticket['steam']}", ""]
    async for m in channel.history(limit=TRANSCRIPT_LIMIT, oldest_first=True):
        text = m.content or ("[сообщение бота]" if m.author.bot else "")
        files = " ".join(a.url for a in m.attachments)
        lines.append(f"[{m.created_at:%d.%m.%Y %H:%M}] {m.author.name}: {text} {files}".rstrip())
    data = io.BytesIO("\n".join(lines).encode("utf-8"))
    try:
        reference = logs.get_partial_message(ticket["log_message"]) if ticket.get("log_message") else None
        await logs.send(file=discord.File(data, filename=f"ticket-{ticket['id']}.txt"),
                        reference=reference, mention_author=False)
    except discord.HTTPException as e:
        log.warning("Ticket %s: cannot save transcript: %s", ticket["id"], e)


async def close_ticket(client: discord.Client, ticket: dict, by: discord.abc.User) -> None:
    """Mark the ticket finished, save the transcript and delete its channel after a short delay."""
    if ticket["status"] not in OPEN_STATUSES:
        return
    ticket["status"] = "closed"
    ticket["closed_by"] = by.id
    ticket["closed"] = int(discord.utils.utcnow().timestamp())
    store.save()
    await refresh_messages(client, ticket)

    channel = client.get_channel(ticket.get("channel") or 0)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(view=note_view(
            f"🔒 Тикет завершён {by.mention}. Канал будет удалён через {DELETE_DELAY} секунд.", 0x80848E),
            allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass

    async def finish():
        await save_transcript(client, ticket, channel)
        await asyncio.sleep(DELETE_DELAY)
        try:
            await channel.delete(reason=f"Ticket #{ticket['id']} closed by {by}")
        except discord.HTTPException as e:
            log.warning("Ticket %s: cannot delete channel: %s", ticket["id"], e)

    asyncio.create_task(finish())


# ---------- components ----------

class OpenTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:open:(?P<game>[a-z0-9]+)"):
    def __init__(self, game: str, emoji: str | discord.PartialEmoji | None = None):
        self.game = game
        name = GAMES.get(game, {"name": game})["name"]
        super().__init__(discord.ui.Button(
            label=f"Тикет {name}", emoji=emoji or None,
            style=discord.ButtonStyle.secondary, custom_id=f"ticket:open:{game}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["game"], item.emoji)

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        game = store.settings(interaction.guild.id)["games"].get(self.game)
        if self.game not in GAMES or not game or not game["enabled"]:
            return await interaction.response.send_message(view=note_view("Эта игра больше недоступна."), ephemeral=True)
        existing = open_ticket_of(interaction.user.id, interaction.guild.id)
        if existing:
            where = f"<#{existing['channel']}>" if existing.get("channel") else f"#{existing['id']}"
            return await interaction.response.send_message(view=note_view(
                f"У вас уже есть открытый тикет: {where}. Завершите его, чтобы создать новый."), ephemeral=True)
        await interaction.response.send_modal(TicketModal(self.game))


class TicketModal(discord.ui.Modal):
    def __init__(self, game: str):
        super().__init__(title=f"Тикет {GAMES[game]['name']}", timeout=None)
        self.game = game
        self.steam = discord.ui.TextInput(
            placeholder="https://steamcommunity.com/profiles/...", max_length=200, required=True,
        )
        self.note = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, placeholder="RU/EU, где нашел? о чем договорился?",
            max_length=1000, required=True,
        )
        self.add_item(discord.ui.Label(text="Steam ссылка или SteamID", component=self.steam))
        self.add_item(discord.ui.Label(text="Примечание для спикера", component=self.note))

    async def on_submit(self, interaction: discord.Interaction):
        guild, user = interaction.guild, interaction.user
        if open_ticket_of(user.id, guild.id):
            return await interaction.response.send_message(
                view=note_view("У вас уже есть открытый тикет."), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)

        settings = store.settings(guild.id)
        category = guild.get_channel(settings["tickets_category"] or 0)
        if not isinstance(category, discord.CategoryChannel):
            panel = guild.get_channel(settings["tickets_channel"] or 0)
            category = panel.category if panel else None

        ticket_id = store.data["next_ticket"]
        store.data["next_ticket"] += 1
        ticket = {
            "id": ticket_id, "guild": guild.id, "guild_name": guild.name, "game": self.game,
            "user": user.id, "username": user.name,
            "steam": clip(self.steam.value.strip(), 200), "note": clip(self.note.value.strip(), 1000),
            "status": "pending", "speaker": None, "closed_by": None,
            "created": int(discord.utils.utcnow().timestamp()),
        }
        ticket["steam64"] = await resolve_steam64(ticket["steam"])
        ticket["previous"] = previous_tickets(ticket)

        # Visible to the author, staff, this game's speakers and the bot
        allow = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                            attach_files=True, embed_links=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                  attach_files=True, embed_links=True, manage_messages=True),
            user: allow,
        }
        for role_id in [*settings["staff_roles"], game_role(ticket)]:
            role = guild.get_role(role_id or 0)
            if role:
                overwrites[role] = allow
        try:
            channel = await guild.create_text_channel(
                name=slug(f"{self.game}-{user.name}")[:90], category=category, overwrites=overwrites,
                topic=f"Тикет #{ticket_id} · {GAMES[self.game]['name']} · {user} ({user.id})",
                reason=f"Ticket #{ticket_id} by {user}",
            )
        except discord.Forbidden:
            store.data["next_ticket"] -= 1
            return await interaction.followup.send(view=note_view(
                "У бота нет прав создавать каналы в категории тикетов. Сообщите администрации."), ephemeral=True)

        ticket["channel"] = channel.id
        ticket["channel_url"] = channel.jump_url
        role = game_role(ticket)
        pings = discord.AllowedMentions(users=[user], roles=[discord.Object(role)] if role else [], everyone=False)
        message = await channel.send(view=ticket_view(ticket), allowed_mentions=pings)
        ticket["message"] = message.id

        logs = guild.get_channel(settings["ticket_logs_channel"] or 0)
        if isinstance(logs, discord.TextChannel):
            try:
                card = await logs.send(view=log_view(ticket), allowed_mentions=NO_PINGS)
                ticket["log_channel"], ticket["log_message"] = logs.id, card.id
            except discord.HTTPException as e:
                log.warning("Ticket %s: cannot post log card: %s", ticket_id, e)

        store.data["tickets"][str(ticket_id)] = ticket
        store.save()
        await interaction.followup.send(view=note_view(f"✅ Тикет создан: {channel.mention}"), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("Ticket modal failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(view=note_view("Не удалось создать тикет, попробуйте позже."), ephemeral=True)


class TakeTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:take:(?P<tid>\d+)"):
    def __init__(self, ticket_id: int):
        self.ticket_id = ticket_id
        super().__init__(discord.ui.Button(label="Взять тикет", style=discord.ButtonStyle.success,
                                           custom_id=f"ticket:take:{ticket_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction):
        ticket = store.data["tickets"].get(str(self.ticket_id))
        if not ticket or ticket["status"] != "pending":
            return await interaction.response.send_message(view=note_view("Тикет уже взят или завершён."), ephemeral=True)
        if interaction.user.id == ticket["user"]:
            return await interaction.response.send_message(view=note_view("Нельзя взять свой тикет."), ephemeral=True)
        if not is_speaker(interaction.user, ticket):
            return await interaction.response.send_message(view=note_view("Брать тикеты могут только спикеры."), ephemeral=True)
        await interaction.response.send_message(
            view=take_request_view(ticket, interaction.user.id),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(ticket["user"])], roles=False, everyone=False))


class AgreeTakeButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"ticket:agree:(?P<tid>\d+):(?P<sid>\d+)"):
    def __init__(self, ticket_id: int, speaker_id: int):
        self.ticket_id = ticket_id
        self.speaker_id = speaker_id
        super().__init__(discord.ui.Button(label="Согласиться", style=discord.ButtonStyle.success,
                                           custom_id=f"ticket:agree:{ticket_id}:{speaker_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["tid"]), int(match["sid"]))

    async def callback(self, interaction: discord.Interaction):
        ticket = store.data["tickets"].get(str(self.ticket_id))
        if not ticket or interaction.user.id != ticket["user"]:
            return await interaction.response.send_message(view=note_view("Согласиться может только автор тикета."), ephemeral=True)
        if ticket["status"] != "pending":
            return await interaction.response.send_message(view=note_view("Тикет уже взят или завершён."), ephemeral=True)
        ticket["status"] = "accepted"
        ticket["speaker"] = self.speaker_id
        store.save()
        await interaction.response.edit_message(view=take_request_view(ticket, self.speaker_id, agreed=True),
                                                allowed_mentions=NO_PINGS)
        await refresh_messages(interaction.client, ticket)


class CloseTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:close:(?P<tid>\d+)"):
    def __init__(self, ticket_id: int):
        self.ticket_id = ticket_id
        super().__init__(discord.ui.Button(label="Завершить тикет", style=discord.ButtonStyle.danger,
                                           custom_id=f"ticket:close:{ticket_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction):
        ticket = store.data["tickets"].get(str(self.ticket_id))
        if not ticket or ticket["status"] not in OPEN_STATUSES:
            return await interaction.response.send_message(view=note_view("Тикет уже завершён."), ephemeral=True)
        if interaction.user.id != ticket["user"] and not is_speaker(interaction.user, ticket):
            return await interaction.response.send_message(view=note_view("Недостаточно прав."), ephemeral=True)
        await interaction.response.defer()
        await close_ticket(interaction.client, ticket, interaction.user)


# ---------- cog ----------

class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_vex_startup(self):
        # Re-post panels that were deleted while the bot was offline
        for guild in self.bot.guilds:
            try:
                await post_panel(self.bot, guild.id)
            except discord.HTTPException as e:
                log.warning("Ticket panel in %s: %s", guild.id, e)
        # Tickets whose channel was deleted while the bot was offline
        for ticket in store.data["tickets"].values():
            if ticket["status"] in OPEN_STATUSES and ticket.get("channel") and self.bot.get_channel(ticket["channel"]) is None \
                    and self.bot.get_guild(ticket.get("guild") or 0):
                ticket["status"] = "closed"
                await refresh_messages(self.bot, ticket)
        store.save()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        ticket = ticket_by_channel(channel.id)
        if ticket and ticket["status"] in OPEN_STATUSES:
            ticket["status"] = "closed"
            store.save()
            await refresh_messages(self.bot, ticket)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(OpenTicketButton, TakeTicketButton, AgreeTakeButton, CloseTicketButton)
    await bot.add_cog(Tickets(bot))
