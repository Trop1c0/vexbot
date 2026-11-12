import logging

import discord
from discord.ext import commands

import config
from config import GAMES
from storage import store

log = logging.getLogger("vex.tickets")

NO_PINGS = discord.AllowedMentions.none()

# status -> (emoji, label, container accent)
STATUS = {
    "pending": ("🕓", "Ожидает", 0xF0B232),
    "accepted": ("✅", "Принят", 0x23A55A),
    "declined": ("⛔", "Отклонен", 0xDA373C),
    "cancelled": ("❌", "Отменен", 0x80848E),
    "closed": ("🔒", "Закрыт", 0x80848E),
}
OPEN_STATUSES = {"pending", "accepted"}
# Staff messages in a ticket thread starting with this prefix stay internal
INTERNAL_PREFIX = "//"


# ---------- helpers ----------

def open_ticket_of(user_id: int) -> dict | None:
    for ticket in store.data["tickets"].values():
        if ticket["user"] == user_id and ticket["status"] in OPEN_STATUSES:
            return ticket
    return None


def ticket_by_thread(thread_id: int) -> dict | None:
    for ticket in store.data["tickets"].values():
        if ticket.get("thread") == thread_id:
            return ticket
    return None


def is_staff(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_messages:
        return True
    staff = store.settings(member.guild.id)["staff_roles"]
    return any(role.id in staff for role in member.roles)


def status_line(ticket: dict) -> str:
    emoji, label, _ = STATUS[ticket["status"]]
    line = f"{emoji} Статус: **{label}**"
    if ticket.get("status_by"):
        line += f" · <@{ticket['status_by']}>"
    return line


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


# ---------- views ----------

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


def log_view(ticket: dict) -> discord.ui.LayoutView:
    game = GAMES[ticket["game"]]["name"]
    header = f"### {game} тикет от <@{ticket['user']}>"
    container = discord.ui.Container(accent_color=STATUS[ticket["status"]][2])
    if ticket.get("thread_url"):
        container.add_item(discord.ui.Section(
            header,
            accessory=discord.ui.Button(label="Сообщения", url=ticket["thread_url"], style=discord.ButtonStyle.link),
        ))
    else:
        container.add_item(discord.ui.TextDisplay(header))
    container.add_item(discord.ui.TextDisplay(quote(status_line(ticket))))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(
        f"**Пользователь:** <@{ticket['user']}> (`{ticket['username']}`, `{ticket['user']}`)\n"
        f"**Steam:** {ticket['steam']}\n"
        f"**Примечание для спикера:**\n{quote(ticket['note'])}"
    ))
    container.add_item(discord.ui.TextDisplay(f"-# Тикет #{ticket['id']} · создан <t:{ticket['created']}:f>"))
    if ticket["status"] in OPEN_STATUSES:
        buttons = []
        if ticket["status"] == "pending":
            buttons.append(TicketActionButton("accept", ticket["id"]))
        buttons += [TicketActionButton("decline", ticket["id"]), TicketActionButton("close", ticket["id"])]
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(*buttons))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def dm_view(ticket: dict) -> discord.ui.LayoutView:
    game = GAMES[ticket["game"]]["name"]
    container = discord.ui.Container(accent_color=STATUS[ticket["status"]][2])
    container.add_item(discord.ui.TextDisplay(
        f"### Тикет {game} #{ticket['id']}\n-# Сервер: {ticket.get('guild_name', '—')}"))
    container.add_item(discord.ui.TextDisplay(quote(status_line(ticket))))
    if ticket["status"] in OPEN_STATUSES:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "Пишите в этот чат — сообщения получит спикер. Его ответы придут сюда же."
        ))
        container.add_item(discord.ui.ActionRow(CancelTicketButton(ticket["id"])))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def note_view(text: str, accent: int | None = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=accent))
    return view


# ---------- status changes ----------

async def set_status(client: discord.Client, ticket: dict, status: str, by: discord.abc.User,
                     edit_log: bool = True, edit_dm: bool = True) -> None:
    ticket["status"] = status
    ticket["status_by"] = by.id
    store.save()
    emoji, label, accent = STATUS[status]

    if edit_log:
        channel = client.get_partial_messageable(ticket["log_channel"])
        try:
            await channel.get_partial_message(ticket["log_message"]).edit(view=log_view(ticket), allowed_mentions=NO_PINGS)
        except discord.HTTPException as e:
            log.warning("Ticket %s: cannot update log message: %s", ticket["id"], e)

    if edit_dm and ticket.get("dm_channel"):
        channel = client.get_partial_messageable(ticket["dm_channel"], type=discord.ChannelType.private)
        try:
            await channel.get_partial_message(ticket["dm_message"]).edit(view=dm_view(ticket))
        except discord.HTTPException as e:
            log.warning("Ticket %s: cannot update DM message: %s", ticket["id"], e)

    # Notify the user unless they changed the status themselves
    if by.id != ticket["user"]:
        try:
            user = client.get_user(ticket["user"]) or await client.fetch_user(ticket["user"])
            await user.send(view=note_view(f"{emoji} Статус вашего тикета #{ticket['id']}: **{label}**", accent))
        except discord.HTTPException:
            pass

    thread = client.get_channel(ticket.get("thread") or 0)
    if isinstance(thread, discord.Thread):
        try:
            await thread.send(view=note_view(f"{emoji} Статус: **{label}** · {by.mention}", accent),
                              allowed_mentions=NO_PINGS)
            if status not in OPEN_STATUSES:
                await thread.edit(archived=True, locked=True)
        except discord.HTTPException as e:
            log.warning("Ticket %s: cannot update thread: %s", ticket["id"], e)


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
        existing = open_ticket_of(interaction.user.id)
        if existing:
            return await interaction.response.send_message(view=note_view(
                f"У вас уже есть открытый тикет #{existing['id']} (сервер **{existing.get('guild_name', '—')}**).\n"
                "Отмените его в ЛС бота, чтобы создать новый."
            ), ephemeral=True)
        await interaction.response.send_modal(TicketModal(self.game))


class TicketModal(discord.ui.Modal):
    def __init__(self, game: str):
        super().__init__(title=f"Тикет {GAMES[game]['name']}", timeout=None)
        self.game = game
        self.steam = discord.ui.TextInput(
            placeholder="https://steamcommunity.com/id/...", max_length=200, required=True,
        )
        self.note = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, placeholder="RU/EU, где нашел? о чем договорился?",
            max_length=1000, required=True,
        )
        self.add_item(discord.ui.Label(text="Steam ссылка или SteamID", component=self.steam))
        self.add_item(discord.ui.Label(text="Примечание для спикера", component=self.note))

    async def on_submit(self, interaction: discord.Interaction):
        client = interaction.client
        if open_ticket_of(interaction.user.id):
            return await interaction.response.send_message(
                view=note_view("У вас уже есть открытый тикет."), ephemeral=True)
        log_channel = client.get_channel(store.settings(interaction.guild.id)["ticket_logs_channel"] or 0)
        if not isinstance(log_channel, discord.TextChannel) or log_channel.guild != interaction.guild:
            return await interaction.response.send_message(
                view=note_view("Тикеты временно недоступны: канал логов не настроен."), ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user
        ticket_id = store.data["next_ticket"]
        store.data["next_ticket"] += 1
        ticket = {
            "id": ticket_id,
            "guild": interaction.guild.id,
            "guild_name": interaction.guild.name,
            "game": self.game,
            "user": user.id,
            "username": user.name,
            "steam": clip(self.steam.value.strip(), 200),
            "note": clip(self.note.value.strip(), 1000),
            "status": "pending",
            "status_by": None,
            "created": int(discord.utils.utcnow().timestamp()),
            "log_channel": log_channel.id,
        }

        message = await log_channel.send(view=log_view(ticket), allowed_mentions=NO_PINGS)
        ticket["log_message"] = message.id
        thread = await message.create_thread(
            name=clip(f"MSG {GAMES[self.game]['name']} Ticket {user.name}", 100), auto_archive_duration=10080,
        )
        ticket["thread"] = thread.id
        ticket["thread_url"] = thread.jump_url
        store.data["tickets"][str(ticket_id)] = ticket
        store.save()
        await message.edit(view=log_view(ticket), allowed_mentions=NO_PINGS)
        await thread.send(view=note_view(
            "Сообщения пользователя из ЛС бота появляются здесь.\n"
            "Всё, что вы пишете в эту ветку, бот перешлёт пользователю в ЛС.\n"
            f"-# Сообщения, начинающиеся с `{INTERNAL_PREFIX}`, не пересылаются."
        ))

        dm_ok = True
        try:
            dm = await user.send(view=dm_view(ticket))
            ticket["dm_channel"] = dm.channel.id
            ticket["dm_message"] = dm.id
            store.save()
        except discord.HTTPException:
            dm_ok = False

        text = f"✅ Тикет #{ticket_id} создан. Спикер ответит вам в личных сообщениях бота."
        if not dm_ok:
            text += "\n⚠️ Не удалось написать вам в ЛС — откройте личные сообщения от участников сервера."
        await interaction.followup.send(view=note_view(text), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("Ticket modal failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(view=note_view("Не удалось создать тикет, попробуйте позже."), ephemeral=True)


ACTIONS = {
    "accept": ("Принять", discord.ButtonStyle.success, "accepted"),
    "decline": ("Отклонить", discord.ButtonStyle.danger, "declined"),
    "close": ("Закрыть", discord.ButtonStyle.secondary, "closed"),
}


class TicketActionButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"ticket:(?P<action>accept|decline|close):(?P<tid>\d+)"):
    def __init__(self, action: str, ticket_id: int):
        self.action = action
        self.ticket_id = ticket_id
        label, style, _ = ACTIONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"ticket:{action}:{ticket_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["tid"]))

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(view=note_view("Недостаточно прав."), ephemeral=True)
        ticket = store.data["tickets"].get(str(self.ticket_id))
        if not ticket or ticket["status"] not in OPEN_STATUSES:
            return await interaction.response.send_message(view=note_view("Тикет уже закрыт."), ephemeral=True)
        ticket["status"] = ACTIONS[self.action][2]
        ticket["status_by"] = interaction.user.id
        await interaction.response.edit_message(view=log_view(ticket), allowed_mentions=NO_PINGS)
        await set_status(interaction.client, ticket, ticket["status"], interaction.user, edit_log=False)


class CancelTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:cancel:(?P<tid>\d+)"):
    def __init__(self, ticket_id: int):
        self.ticket_id = ticket_id
        super().__init__(discord.ui.Button(
            label="Отменить тикет", emoji="✖️", style=discord.ButtonStyle.danger, custom_id=f"ticket:cancel:{ticket_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction):
        ticket = store.data["tickets"].get(str(self.ticket_id))
        if not ticket or ticket["user"] != interaction.user.id:
            return await interaction.response.send_message(view=note_view("Это не ваш тикет."), ephemeral=True)
        if ticket["status"] not in OPEN_STATUSES:
            return await interaction.response.send_message(view=note_view("Тикет уже закрыт."), ephemeral=True)
        ticket["status"] = "cancelled"
        ticket["status_by"] = interaction.user.id
        await interaction.response.edit_message(view=dm_view(ticket))
        await set_status(interaction.client, ticket, "cancelled", interaction.user, edit_dm=False)


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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None:
            await self.relay_from_user(message)
        elif isinstance(message.channel, discord.Thread):
            await self.relay_from_staff(message)

    async def relay_from_user(self, message: discord.Message):
        ticket = open_ticket_of(message.author.id)
        if not ticket:
            return
        thread = self.bot.get_channel(ticket.get("thread") or 0)
        if not isinstance(thread, discord.Thread):
            return
        body = "\n".join(filter(None, [message.content, *(a.url for a in message.attachments)]))
        if not body:
            return
        try:
            if thread.archived:
                await thread.edit(archived=False)
            await thread.send(clip(f"**{message.author.name}**: {body}", 2000), allowed_mentions=NO_PINGS)
        except discord.HTTPException as e:
            log.warning("Ticket %s: relay to thread failed: %s", ticket["id"], e)
            await message.add_reaction("⚠️")

    async def relay_from_staff(self, message: discord.Message):
        ticket = ticket_by_thread(message.channel.id)
        if not ticket or message.content.startswith(INTERNAL_PREFIX):
            return
        if ticket["status"] not in OPEN_STATUSES:
            return
        body = "\n".join(filter(None, [message.content, *(a.url for a in message.attachments)]))
        if not body:
            return
        try:
            user = self.bot.get_user(ticket["user"]) or await self.bot.fetch_user(ticket["user"])
            await user.send(view=note_view(clip(f"**{message.author.display_name}** (спикер):\n{body}", 3900)))
        except discord.HTTPException:
            await message.add_reaction("⚠️")  # user has DMs closed


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(OpenTicketButton, TicketActionButton, CancelTicketButton)
    await bot.add_cog(Tickets(bot))
