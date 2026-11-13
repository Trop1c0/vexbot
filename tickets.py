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
# outcome chosen when finishing a ticket -> (emoji, label)
RESULTS = {
    "accepted": ("✅", "Принят"),
    "declined": ("⛔", "Отклонён"),
    "cancelled": ("↩️", "Отменён автором"),
}
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


def stars(n: int) -> str:
    return "⭐" * n + "☆" * (5 - n)


def result_text(ticket: dict) -> str:
    """«Итог: Принят» + reason, for closed tickets."""
    if ticket.get("result") not in RESULTS:
        return ""
    emoji, label = RESULTS[ticket["result"]]
    text = f"**Итог:** {emoji} {label}"
    if ticket.get("reason"):
        text += f"\n**Причина:** {ticket['reason']}"
    return text


def speaker_stats(guild_id: int, since: int = 0) -> list[dict]:
    """Per-speaker numbers for the panel: taken / results / rating / timings."""
    per: dict[int, dict] = {}
    for t in store.data["tickets"].values():
        if t.get("guild") != guild_id or not t.get("speaker") or t["created"] < since:
            continue
        s = per.setdefault(t["speaker"], {"speaker": t["speaker"], "taken": 0, "open": 0, "accepted": 0,
                                          "declined": 0, "cancelled": 0, "ratings": [], "response": [], "duration": []})
        s["taken"] += 1
        if t["status"] in OPEN_STATUSES:
            s["open"] += 1
        if t.get("result") in RESULTS:
            s[t["result"]] += 1
        if t.get("rating"):
            s["ratings"].append(t["rating"])
        if t.get("taken_at"):
            s["response"].append(t["taken_at"] - t["created"])
            if t.get("closed"):
                s["duration"].append(t["closed"] - t["taken_at"])
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None  # noqa: E731
    result = []
    for s in per.values():
        result.append({
            "speaker": s["speaker"], "taken": s["taken"], "open": s["open"], "accepted": s["accepted"],
            "declined": s["declined"], "cancelled": s["cancelled"],
            "rating": avg(s["ratings"]), "ratings": len(s["ratings"]),
            "response": avg(s["response"]), "duration": avg(s["duration"]),
        })
    return sorted(result, key=lambda s: (-s["taken"], -(s["rating"] or 0)))


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
        container.add_item(discord.ui.TextDisplay(f"🔒 Тикет завершён{by}\n{result_text(ticket)}".rstrip()))

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
        buttons = [TakeTicketButton(ticket["id"]) if status == "pending" else TransferTicketButton(ticket["id"])]
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
    extra = result_text(ticket)
    if ticket.get("rating"):
        extra += f"\n**Оценка спикера:** {stars(ticket['rating'])} ({ticket['rating']}/5)"
    container.add_item(discord.ui.TextDisplay(quote(status) + (f"\n{extra.strip()}" if extra.strip() else "")))
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


def log_thread(client: discord.Client, ticket: dict) -> discord.Thread | None:
    thread = client.get_channel(ticket.get("log_thread") or 0)
    return thread if isinstance(thread, discord.Thread) else None


async def log_event(client: discord.Client, ticket: dict, text: str) -> None:
    """A status line in the ticket's log thread (taken, finished, ...)."""
    thread = log_thread(client, ticket)
    if thread is None:
        return
    try:
        await thread.send(view=note_view(text, STATUS[ticket["status"]][2]), allowed_mentions=NO_PINGS)
    except discord.HTTPException as e:
        log.warning("Ticket %s: cannot write to log thread: %s", ticket["id"], e)


async def relay_to_log(client: discord.Client, ticket: dict, message: discord.Message) -> None:
    """Copy a message from the ticket channel into the log thread as «username: text»."""
    thread = log_thread(client, ticket)
    if thread is None:
        return
    parts = [message.content, *(a.url for a in message.attachments), *(f"[стикер: {s.name}]" for s in message.stickers)]
    body = "\n".join(p for p in parts if p)
    if not body:
        return
    try:
        await thread.send(clip(f"**{message.author.name}**: {body}", 2000), allowed_mentions=NO_PINGS)
    except discord.HTTPException as e:
        log.warning("Ticket %s: cannot relay message: %s", ticket["id"], e)


async def save_transcript(client: discord.Client, ticket: dict, channel: discord.TextChannel) -> None:
    thread = log_thread(client, ticket)
    logs = thread or client.get_channel(ticket.get("log_channel") or 0)
    if not isinstance(logs, (discord.TextChannel, discord.Thread)):
        return
    lines = [f"Тикет #{ticket['id']} · {GAMES[ticket['game']]['name']} · {ticket['username']} ({ticket['user']})",
             f"Steam: {ticket.get('steam64') or ticket['steam']}", ""]
    async for m in channel.history(limit=TRANSCRIPT_LIMIT, oldest_first=True):
        text = m.content or ("[сообщение бота]" if m.author.bot else "")
        files = " ".join(a.url for a in m.attachments)
        lines.append(f"[{m.created_at:%d.%m.%Y %H:%M}] {m.author.name}: {text} {files}".rstrip())
    data = io.BytesIO("\n".join(lines).encode("utf-8"))
    try:
        reference = None
        if thread is None and ticket.get("log_message"):
            reference = logs.get_partial_message(ticket["log_message"])
        await logs.send(file=discord.File(data, filename=f"ticket-{ticket['id']}.txt"),
                        reference=reference, mention_author=False)
    except discord.HTTPException as e:
        log.warning("Ticket %s: cannot save transcript: %s", ticket["id"], e)


async def lock_log_thread(client: discord.Client, ticket: dict) -> None:
    thread = log_thread(client, ticket)
    if thread is not None:
        try:
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException as e:
            log.warning("Ticket %s: cannot lock log thread: %s", ticket["id"], e)


def rating_view(ticket: dict, rated: int | None = None) -> discord.ui.LayoutView:
    """DM to the author asking to rate the speaker."""
    game = GAMES[ticket["game"]]["name"]
    container = discord.ui.Container(accent_color=0xF0B232)
    container.add_item(discord.ui.TextDisplay(
        f"### Оцените спикера\nТикет {game} #{ticket['id']} на сервере **{ticket.get('guild_name', '—')}** "
        f"завершён. Как вам работа спикера <@{ticket['speaker']}>?\n{result_text(ticket)}".rstrip()))
    if rated:
        container.add_item(discord.ui.TextDisplay(f"Спасибо! Ваша оценка: {stars(rated)}"))
    else:
        container.add_item(discord.ui.ActionRow(*(RateButton(ticket["id"], n) for n in range(1, 6))))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


async def ask_rating(client: discord.Client, ticket: dict) -> None:
    if not ticket.get("speaker") or ticket.get("rating"):
        return
    try:
        user = client.get_user(ticket["user"]) or await client.fetch_user(ticket["user"])
        await user.send(view=rating_view(ticket))
    except discord.HTTPException:
        pass  # DMs closed


async def close_ticket(client: discord.Client, ticket: dict, by: discord.abc.User,
                       result: str | None = None, reason: str = "") -> None:
    """Mark the ticket finished, save the transcript and delete its channel after a short delay."""
    if ticket["status"] not in OPEN_STATUSES:
        return
    if result not in RESULTS:
        result = "cancelled" if by.id == ticket["user"] else None
    ticket["status"] = "closed"
    ticket["closed_by"] = by.id
    ticket["closed"] = int(discord.utils.utcnow().timestamp())
    ticket["result"] = result
    ticket["reason"] = clip(reason.strip(), 500)
    store.save()
    await refresh_messages(client, ticket)

    summary = f"🔒 Тикет завершил **{by.name}**"
    if result:
        summary += f"\nИтог: {RESULTS[result][0]} {RESULTS[result][1]}"
    if ticket["reason"]:
        summary += f"\nПричина: {ticket['reason']}"
    await log_event(client, ticket, summary)
    await ask_rating(client, ticket)

    channel = client.get_channel(ticket.get("channel") or 0)
    if not isinstance(channel, discord.TextChannel):
        await lock_log_thread(client, ticket)
        return
    try:
        await channel.send(view=note_view(
            f"🔒 Тикет завершён {by.mention}.\n{result_text(ticket)}\n-# Канал будет удалён через {DELETE_DELAY} секунд.",
            0x80848E), allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass

    async def finish():
        await save_transcript(client, ticket, channel)
        await lock_log_thread(client, ticket)
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
                # Everything written in the ticket channel is copied into this thread
                thread = await card.create_thread(
                    name=clip(f"Тикет #{ticket_id} · {GAMES[self.game]['name']} · {user.name}", 100),
                    auto_archive_duration=10080,
                )
                ticket["log_thread"] = thread.id
            except discord.HTTPException as e:
                log.warning("Ticket %s: cannot post log card / thread: %s", ticket_id, e)

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
        await log_event(interaction.client, ticket, f"🙋 Спикер **{interaction.user.name}** хочет взять тикет")


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
        ticket["taken_at"] = int(discord.utils.utcnow().timestamp())
        store.save()
        await interaction.response.edit_message(view=take_request_view(ticket, self.speaker_id, agreed=True),
                                                allowed_mentions=NO_PINGS)
        await refresh_messages(interaction.client, ticket)
        speaker = interaction.guild.get_member(self.speaker_id)
        name = speaker.name if speaker else str(self.speaker_id)
        await log_event(interaction.client, ticket, f"✅ Тикет взял спикер **{name}** (автор **{interaction.user.name}** согласился)")


async def transfer_ticket(client: discord.Client, ticket: dict, by: discord.Member, target: discord.Member) -> None:
    """Hand an accepted ticket over to another speaker (no author consent needed)."""
    old = ticket.get("speaker")
    ticket["speaker"] = target.id
    ticket.setdefault("transfers", []).append({
        "from": old, "to": target.id, "by": by.id, "at": int(discord.utils.utcnow().timestamp()),
    })
    store.save()

    channel = client.get_channel(ticket.get("channel") or 0)
    if isinstance(channel, discord.TextChannel):
        try:
            # make sure the new speaker sees the channel even without a speaker role overwrite
            await channel.set_permissions(target, view_channel=True, send_messages=True, read_message_history=True,
                                          attach_files=True, embed_links=True)
            await channel.send(
                view=note_view(f"🔁 <@{by.id}> передал тикет спикеру <@{target.id}>", 0x5865F2),
                allowed_mentions=discord.AllowedMentions(users=[target], roles=False, everyone=False))
        except discord.HTTPException as e:
            log.warning("Ticket %s: transfer message failed: %s", ticket["id"], e)
    await refresh_messages(client, ticket)
    old_member = channel.guild.get_member(old) if isinstance(channel, discord.TextChannel) and old else None
    old_name = old_member.name if old_member else (str(old) if old else "—")
    await log_event(client, ticket, f"🔁 **{by.name}** передал тикет: **{old_name}** → **{target.name}**")


class TransferPickView(discord.ui.LayoutView):
    """Ephemeral picker: who gets the ticket."""

    def __init__(self, ticket_id: int):
        super().__init__(timeout=120)
        self.ticket_id = ticket_id
        self.select = discord.ui.UserSelect(placeholder="Выберите спикера", min_values=1, max_values=1)
        self.select.callback = self.picked
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"**Кому передать тикет #{ticket_id}?**\n"
                                   "-# Нужна роль спикеров этой игры или роль администрации."),
            discord.ui.ActionRow(self.select),
        ))

    async def picked(self, interaction: discord.Interaction):
        ticket = store.data["tickets"].get(str(self.ticket_id))
        if not ticket or ticket["status"] != "accepted":
            return await interaction.response.edit_message(view=note_view("Тикет уже завершён."))
        if interaction.user.id != ticket.get("speaker") and not is_staff(interaction.user):
            return await interaction.response.edit_message(view=note_view("Передать тикет может только его спикер."))
        target = self.select.values[0]
        if not isinstance(target, discord.Member) or target.bot:
            return await interaction.response.edit_message(view=note_view("Выберите участника сервера."))
        if target.id == ticket["user"]:
            return await interaction.response.edit_message(view=note_view("Нельзя передать тикет его автору."))
        if target.id == ticket.get("speaker"):
            return await interaction.response.edit_message(view=note_view("Этот спикер уже ведёт тикет."))
        if not is_speaker(target, ticket):
            return await interaction.response.edit_message(view=note_view(
                f"У {target.mention} нет роли спикеров этой игры или администрации."))
        await interaction.response.edit_message(view=note_view(f"✅ Тикет передан {target.mention}"))
        await transfer_ticket(interaction.client, ticket, interaction.user, target)


class TransferTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:transfer:(?P<tid>\d+)"):
    def __init__(self, ticket_id: int):
        self.ticket_id = ticket_id
        super().__init__(discord.ui.Button(label="Передать тикет", emoji="🔁", style=discord.ButtonStyle.secondary,
                                           custom_id=f"ticket:transfer:{ticket_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction):
        ticket = store.data["tickets"].get(str(self.ticket_id))
        if not ticket or ticket["status"] != "accepted":
            return await interaction.response.send_message(view=note_view("Передать можно только взятый тикет."), ephemeral=True)
        if interaction.user.id != ticket.get("speaker") and not is_staff(interaction.user):
            return await interaction.response.send_message(view=note_view("Передать тикет может только его спикер."), ephemeral=True)
        await interaction.response.send_message(view=TransferPickView(self.ticket_id), ephemeral=True)


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
        speaker = is_speaker(interaction.user, ticket) and interaction.user.id != ticket["user"]
        if interaction.user.id != ticket["user"] and not speaker:
            return await interaction.response.send_message(view=note_view("Недостаточно прав."), ephemeral=True)
        # Speakers choose the outcome; the author only gives a reason (outcome: cancelled by author)
        await interaction.response.send_modal(CloseTicketModal(self.ticket_id, with_result=speaker))


class CloseTicketModal(discord.ui.Modal):
    def __init__(self, ticket_id: int, with_result: bool):
        super().__init__(title=f"Завершить тикет #{ticket_id}", timeout=None)
        self.ticket_id = ticket_id
        self.result: discord.ui.Select | None = None
        if with_result:
            self.result = discord.ui.Select(options=[
                discord.SelectOption(label="Принят", value="accepted", emoji="✅"),
                discord.SelectOption(label="Отклонён", value="declined", emoji="⛔"),
            ], required=True)
            self.add_item(discord.ui.Label(text="Итог", component=self.result))
        self.reason = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, max_length=500, required=with_result,
            placeholder="Почему принят / отклонён" if with_result else "Почему отменяете тикет (необязательно)",
        )
        self.add_item(discord.ui.Label(text="Причина", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction):
        ticket = store.data["tickets"].get(str(self.ticket_id))
        if not ticket or ticket["status"] not in OPEN_STATUSES:
            return await interaction.response.send_message(view=note_view("Тикет уже завершён."), ephemeral=True)
        result = self.result.values[0] if self.result and self.result.values else "cancelled"
        await interaction.response.defer()
        await close_ticket(interaction.client, ticket, interaction.user, result, self.reason.value or "")


class RateButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:rate:(?P<tid>\d+):(?P<n>[1-5])"):
    def __init__(self, ticket_id: int, n: int):
        self.ticket_id = ticket_id
        self.n = n
        super().__init__(discord.ui.Button(label=str(n), emoji="⭐", style=discord.ButtonStyle.secondary,
                                           custom_id=f"ticket:rate:{ticket_id}:{n}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["tid"]), int(match["n"]))

    async def callback(self, interaction: discord.Interaction):
        ticket = store.data["tickets"].get(str(self.ticket_id))
        if not ticket or interaction.user.id != ticket["user"]:
            return await interaction.response.send_message(view=note_view("Это не ваш тикет."), ephemeral=True)
        if ticket.get("rating"):
            return await interaction.response.edit_message(view=rating_view(ticket, ticket["rating"]))
        ticket["rating"] = self.n
        ticket["rated_at"] = int(discord.utils.utcnow().timestamp())
        store.save()
        await interaction.response.edit_message(view=rating_view(ticket, self.n))
        await refresh_messages(interaction.client, ticket)  # shows the rating on the log card


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
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        ticket = ticket_by_channel(message.channel.id)
        if ticket and ticket.get("log_thread"):
            await relay_to_log(self.bot, ticket, message)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        ticket = ticket_by_channel(channel.id)
        if ticket and ticket["status"] in OPEN_STATUSES:
            ticket["status"] = "closed"
            store.save()
            await refresh_messages(self.bot, ticket)
            await log_event(self.bot, ticket, "🗑️ Канал тикета удалили вручную")
            await lock_log_thread(self.bot, ticket)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(OpenTicketButton, TakeTicketButton, AgreeTakeButton, TransferTicketButton,
                          CloseTicketButton, RateButton)
    await bot.add_cog(Tickets(bot))
