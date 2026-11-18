"""Internal HTTP API for the PHP panel (panel/). Not for browsers.

The panel handles Discord login and sessions, then calls these endpoints with the shared API_SECRET
and the logged-in user's id in X-User-Id. The bot itself checks that this user may manage the server.
"""
import hmac
import json
import logging
import re

import discord
from aiohttp import web
from discord.ext import commands

import config
import message_builder
import tickets
import voice
from config import GAMES
from storage import store

log = logging.getLogger("vex.api")

BANNER_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
BANNER_MAX = 8 * 1024 * 1024

BOT_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, send_messages_in_threads=True, create_public_threads=True,
    manage_threads=True, embed_links=True, attach_files=True, read_message_history=True, add_reactions=True,
    manage_channels=True, manage_roles=True, move_members=True, connect=True,
)


def fail(status: int, message: str) -> web.HTTPException:
    cls = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 403: web.HTTPForbidden,
           404: web.HTTPNotFound, 502: web.HTTPBadGateway, 503: web.HTTPServiceUnavailable}[status]
    return cls(text=json.dumps({"error": message}, ensure_ascii=False), content_type="application/json")


def sid(value) -> str | None:
    return str(value) if value else None


def parse_id(value) -> int | None:
    if value in (None, "", 0):
        return None
    value = str(value)
    if not value.isdigit():
        raise fail(400, "Неверный ID")
    return int(value)


class PanelAPI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.runner: web.AppRunner | None = None

    # ---------- lifecycle ----------

    async def cog_load(self):
        app = web.Application(middlewares=[self.guard], client_max_size=BANNER_MAX + 1024 * 1024)
        r = app.router
        r.add_get("/internal/bot", self.bot_info)
        r.add_post("/internal/lookup", self.lookup)
        r.add_get("/internal/guilds/{gid}", self.guild_info)
        r.add_patch("/internal/guilds/{gid}/settings", self.update_settings)
        r.add_post("/internal/guilds/{gid}/panel", self.repost_panel)
        r.add_get("/internal/guilds/{gid}/banner", self.get_banner)
        r.add_post("/internal/guilds/{gid}/banner", self.upload_banner)
        r.add_delete("/internal/guilds/{gid}/banner", self.delete_banner)
        r.add_post("/internal/guilds/{gid}/join-channels", self.add_join_channel)
        r.add_delete("/internal/guilds/{gid}/join-channels/{cid}", self.delete_join_channel)
        r.add_post("/internal/guilds/{gid}/tickets/{tid}", self.ticket_action)
        r.add_post("/internal/guilds/{gid}/messages", self.send_message)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, config.API_HOST, config.API_PORT).start()
        log.info("Panel API listening on %s:%s", config.API_HOST, config.API_PORT)
        if len(config.API_SECRET) < 24:
            log.error("API_SECRET is missing or shorter than 24 characters: the panel will not be able to connect")

    async def cog_unload(self):
        if self.runner:
            await self.runner.cleanup()

    # ---------- middleware / auth ----------

    @web.middleware
    async def guard(self, request: web.Request, handler):
        if len(config.API_SECRET) < 24:
            raise fail(503, "API_SECRET у бота не настроен")
        given = request.headers.get("Authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {config.API_SECRET}".encode()):
            # 503, not 401: a 401 would make the panel send the user back to the login page
            raise fail(503, "Панель не может подключиться к боту: API ключи не совпадают")
        if not self.bot.is_ready():
            raise fail(503, "Бот ещё запускается, попробуйте через пару секунд")
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except discord.Forbidden as e:
            raise fail(403, f"У бота не хватает прав: {e.text or e}")
        except discord.HTTPException as e:
            raise fail(502, f"Discord вернул ошибку: {e.text or e}")
        except Exception:
            log.exception("Panel API error on %s %s", request.method, request.path)
            raise fail(502, "Внутренняя ошибка")

    async def manageable(self, request: web.Request) -> tuple[None, discord.Guild, discord.Member]:
        uid = parse_id(request.headers.get("X-User-Id"))
        if not uid:
            raise fail(401, "Нужно войти через Discord")
        guild = self.bot.get_guild(parse_id(request.match_info["gid"]) or 0)
        if guild is None:
            raise fail(404, "Бот не добавлен на этот сервер")
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                raise fail(403, "Вы не участник этого сервера")
        if not member.guild_permissions.manage_guild:
            raise fail(403, "Нужно право «Управлять сервером»")
        return None, guild, member

    # ---------- api ----------

    def bot_payload(self) -> dict:
        me = self.bot.user
        return {"name": me.name, "avatar": me.display_avatar.url, "servers": len(self.bot.guilds),
                "permissions": str(BOT_PERMISSIONS.value)}

    async def bot_info(self, request: web.Request):
        return web.json_response(self.bot_payload())

    async def lookup(self, request: web.Request):
        """Which of the given servers the bot is on (for the panel's server list)."""
        ids = (await request.json()).get("ids") or []
        found = {}
        for raw in ids[:300]:
            guild = self.bot.get_guild(parse_id(raw) or 0)
            if guild:
                found[str(guild.id)] = {"members": guild.member_count}
        return web.json_response({"bot": self.bot_payload(), "guilds": found})

    def serialize_settings(self, settings: dict) -> dict:
        return {
            "tickets_channel": sid(settings["tickets_channel"]),
            "tickets_category": sid(settings["tickets_category"]),
            "ticket_logs_channel": sid(settings["ticket_logs_channel"]),
            "member_logs_channel": sid(settings["member_logs_channel"]),
            "checker_channel": sid(settings["checker_channel"]),
            "checker_source": settings["checker_source"],
            "auto_check": settings["auto_check"],
            "reminder_enabled": settings["reminder_enabled"],
            "reminder_minutes": settings["reminder_minutes"],
            "reminder_role": sid(settings["reminder_role"]),
            "reminder_max": settings["reminder_max"],
            "log_joins": settings["log_joins"], "log_leaves": settings["log_leaves"],
            "staff_roles": [str(r) for r in settings["staff_roles"]],
            "banner_url": settings["banner_url"],
            "banner_file": bool(settings["banner_file"]),
            "games": {k: {**g, "role": sid(g.get("role"))} for k, g in settings["games"].items()},
        }

    @staticmethod
    def member_name(guild: discord.Guild, user_id: int | None) -> str | None:
        if not user_id:
            return None
        member = guild.get_member(user_id)
        return member.display_name if member else str(user_id)

    @staticmethod
    def stat_periods() -> dict[str, int]:
        now = int(discord.utils.utcnow().timestamp())
        return {"7": now - 7 * 86400, "30": now - 30 * 86400, "all": 0}

    async def guild_info(self, request: web.Request):
        _, guild, _ = await self.manageable(request)
        settings = store.settings(guild.id)
        cat = lambda ch: ch.category.name if ch.category else None  # noqa: E731

        guild_tickets = sorted((t for t in store.data["tickets"].values() if t.get("guild") == guild.id),
                               key=lambda t: t["id"], reverse=True)
        joins = [guild.get_channel(cid) for cid in store.data["join_channels"]]
        joins = [ch for ch in joins if ch and ch.guild == guild]
        rooms = []
        for cid, rec in store.data["temp_channels"].items():
            ch = guild.get_channel(int(cid))
            if ch:
                owner = guild.get_member(rec["owner"])
                rooms.append({"id": cid, "name": ch.name, "members": len(ch.members), "limit": ch.user_limit,
                              "owner": owner.display_name if owner else rec["owner"],
                              "locked": rec["locked"], "hidden": rec["hidden"]})
        panel = store.data["panels"].get(f"tickets:{guild.id}")

        return web.json_response({
            "guild": {"id": str(guild.id), "name": guild.name, "icon": guild.icon.url if guild.icon else None,
                      "members": guild.member_count, "created": int(guild.created_at.timestamp())},
            "settings": self.serialize_settings(settings),
            "channels": {
                "text": [{"id": str(c.id), "name": c.name, "category": cat(c)} for c in guild.text_channels],
                "categories": [{"id": str(c.id), "name": c.name} for c in guild.categories],
            },
            "roles": [{"id": str(r.id), "name": r.name, "color": f"#{r.color.value:06x}" if r.color.value else None}
                      for r in reversed(guild.roles) if not r.is_default() and not r.managed],
            "emojis": [{"id": str(e.id), "name": e.name, "value": str(e), "url": e.url} for e in guild.emojis],
            "games": [{"key": k, "name": v["name"], "emoji": v["emoji"]} for k, v in GAMES.items()],
            "panel": {"channel": sid(panel["channel"]), "url": f"https://discord.com/channels/{guild.id}/{panel['channel']}/{panel['message']}"} if panel else None,
            "tickets": [{
                "id": t["id"], "game": GAMES[t["game"]]["name"], "user": str(t["user"]), "username": t["username"],
                "deal": tickets.DEALS[t["deal"]][0] if t.get("deal") in tickets.DEALS else None,
                "status": t["status"], "created": t["created"],
                "channel_url": t.get("channel_url") if t["status"] in tickets.OPEN_STATUSES else None,
                "log_url": f"https://discord.com/channels/{guild.id}/{t['log_thread']}" if t.get("log_thread") else None,
                "steam": t["steam"], "steam64": t.get("steam64"), "note": t["note"],
                "speaker": self.member_name(guild, t.get("speaker")),
                "result": t.get("result"), "reason": t.get("reason") or "", "rating": t.get("rating"),
            } for t in guild_tickets[:100]],
            "speakers": {period: [
                {**s, "name": self.member_name(guild, s["speaker"]), "speaker": str(s["speaker"])}
                for s in tickets.speaker_stats(guild.id, since)
            ] for period, since in self.stat_periods().items()},
            "join_channels": [{"id": str(c.id), "name": c.name, "category": cat(c)} for c in joins],
            "checker": {"steam_key": bool(config.STEAM_API_KEY), "faceit_key": bool(config.FACEIT_API_KEY),
                        "lzt_key": bool(config.LZT_API_TOKEN)},
            "rooms": rooms,
            "stats": {
                "members": guild.member_count,
                "open_tickets": sum(t["status"] in tickets.OPEN_STATUSES for t in guild_tickets),
                "total_tickets": len(guild_tickets),
                "rooms": len(rooms),
                "in_voice": sum(len(c.members) for c in guild.voice_channels),
            },
        })

    async def update_settings(self, request: web.Request):
        _, guild, _ = await self.manageable(request)
        body = await request.json()
        settings = store.settings(guild.id)
        # only what is visible on the panel message; role changes don't need a re-post
        panel_state = lambda: (settings["tickets_channel"], settings["banner_url"],  # noqa: E731
                               repr([(g["enabled"], g["emoji"]) for g in settings["games"].values()]))
        before = panel_state()

        for key in ("tickets_channel", "ticket_logs_channel", "member_logs_channel", "checker_channel"):
            if key in body:
                cid = parse_id(body[key])
                if cid and not isinstance(guild.get_channel(cid), discord.TextChannel):
                    raise fail(400, "Канал не найден на сервере")
                settings[key] = cid
        for key in ("auto_check", "reminder_enabled"):
            if key in body:
                settings[key] = bool(body[key])
        for key, low, high, title in (("reminder_minutes", 5, 1440, "Интервал напоминаний"),
                                      ("reminder_max", 0, 100, "Лимит напоминаний")):
            if key in body:
                try:
                    value = int(body[key])
                except (TypeError, ValueError):
                    raise fail(400, f"{title}: нужно число")
                if not low <= value <= high:
                    raise fail(400, f"{title}: от {low} до {high}")
                settings[key] = value
        if "reminder_role" in body:
            role = parse_id(body["reminder_role"])
            if role and guild.get_role(role) is None:
                raise fail(400, "Роль для напоминаний не найдена")
            settings["reminder_role"] = role
        if "checker_source" in body:
            if body["checker_source"] not in ("lzt", "steam", "both"):
                raise fail(400, "Неизвестный источник цен")
            settings["checker_source"] = body["checker_source"]
        if "tickets_category" in body:
            cid = parse_id(body["tickets_category"])
            if cid and not isinstance(guild.get_channel(cid), discord.CategoryChannel):
                raise fail(400, "Категория не найдена на сервере")
            settings["tickets_category"] = cid
        for key in ("log_joins", "log_leaves"):
            if key in body:
                settings[key] = bool(body[key])
        if "staff_roles" in body:
            roles = [parse_id(r) for r in body["staff_roles"]]
            if any(guild.get_role(r) is None for r in roles):
                raise fail(400, "Роль не найдена на сервере")
            settings["staff_roles"] = roles
        if "banner_url" in body:
            url = str(body["banner_url"] or "").strip()
            if url and (not url.startswith("https://") or len(url) > 500):
                raise fail(400, "Ссылка на баннер должна начинаться с https://")
            settings["banner_url"] = url
        if "games" in body:
            for key, game in body["games"].items():
                if key not in GAMES:
                    continue
                emoji = str(game.get("emoji") or "").strip()
                if len(emoji) > 80:
                    raise fail(400, "Слишком длинный эмодзи")
                role = parse_id(game.get("role"))
                if role and guild.get_role(role) is None:
                    raise fail(400, "Роль не найдена на сервере")
                settings["games"][key] = {"enabled": bool(game.get("enabled")), "emoji": emoji, "role": role}
            if not any(g["enabled"] for g in settings["games"].values()):
                raise fail(400, "Оставьте включённой хотя бы одну игру")
        store.save()

        warning = None
        after = panel_state()
        if after != before:
            warning = await self.refresh_panel(guild, force=after[0] == before[0])
        return web.json_response({"settings": self.serialize_settings(settings), "warning": warning})

    async def refresh_panel(self, guild: discord.Guild, force: bool = True) -> str | None:
        """Re-posts (or removes) the ticket panel. Returns a warning for the UI instead of failing the save."""
        key = f"tickets:{guild.id}"
        if not store.settings(guild.id)["tickets_channel"]:
            record = store.data["panels"].pop(key, None)
            store.save()
            if record:
                try:
                    await self.bot.get_partial_messageable(record["channel"]).get_partial_message(record["message"]).delete()
                except discord.HTTPException:
                    pass
            return None
        try:
            await tickets.post_panel(self.bot, guild.id, force=force)
        except discord.HTTPException as e:
            return f"Настройки сохранены, но панель не отправлена: {e.text or e}"
        return None

    async def repost_panel(self, request: web.Request):
        _, guild, _ = await self.manageable(request)
        if not store.settings(guild.id)["tickets_channel"]:
            raise fail(400, "Сначала выберите канал для панели тикетов")
        message = await tickets.post_panel(self.bot, guild.id, force=True)
        return web.json_response({"url": message.jump_url if message else None})

    def banner_path(self, guild_id: int):
        name = store.settings(guild_id)["banner_file"]
        return config.BANNERS_DIR / name if name else None

    async def get_banner(self, request: web.Request):
        _, guild, _ = await self.manageable(request)
        path = self.banner_path(guild.id)
        if path is None and not store.settings(guild.id)["banner_url"]:
            path = config.DEFAULT_BANNER
        if path is None or not path.exists():
            raise fail(404, "Нет баннера")
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def upload_banner(self, request: web.Request):
        _, guild, _ = await self.manageable(request)
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "file":
            raise fail(400, "Файл не получен")
        ext = BANNER_TYPES.get(part.headers.get("Content-Type", ""))
        if not ext:
            raise fail(400, "Нужна картинка PNG, JPG, GIF или WEBP")
        data = bytearray()
        while chunk := await part.read_chunk():
            data += chunk
            if len(data) > BANNER_MAX:
                raise fail(400, "Файл больше 8 МБ")
        old = self.banner_path(guild.id)
        if old and old.exists():
            old.unlink()
        config.BANNERS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{guild.id}{ext}"
        (config.BANNERS_DIR / name).write_bytes(bytes(data))
        settings = store.settings(guild.id)
        settings["banner_file"] = name
        store.save()
        warning = await self.refresh_panel(guild)
        return web.json_response({"settings": self.serialize_settings(settings), "warning": warning})

    async def delete_banner(self, request: web.Request):
        _, guild, _ = await self.manageable(request)
        path = self.banner_path(guild.id)
        if path and path.exists():
            path.unlink()
        settings = store.settings(guild.id)
        settings["banner_file"] = None
        store.save()
        warning = await self.refresh_panel(guild)
        return web.json_response({"settings": self.serialize_settings(settings), "warning": warning})

    async def add_join_channel(self, request: web.Request):
        _, guild, member = await self.manageable(request)
        body = await request.json()
        category = guild.get_channel(parse_id(body.get("category")) or 0)
        if not isinstance(category, discord.CategoryChannel):
            raise fail(400, "Выберите категорию")
        channel = await voice.create_join_channel(category, str(body.get("name") or ""), f"Join to create by {member} (dashboard)")
        return web.json_response({"id": str(channel.id), "name": channel.name})

    async def delete_join_channel(self, request: web.Request):
        _, guild, member = await self.manageable(request)
        cid = parse_id(request.match_info["cid"])
        channel = guild.get_channel(cid or 0)
        if channel is None or cid not in store.data["join_channels"]:
            raise fail(404, "Канал не найден")
        await channel.delete(reason=f"Removed from dashboard by {member}")
        return web.json_response({"ok": True})

    async def ticket_action(self, request: web.Request):
        _, guild, member = await self.manageable(request)
        ticket = store.data["tickets"].get(request.match_info["tid"])
        if not ticket or ticket.get("guild") != guild.id:
            raise fail(404, "Тикет не найден")
        body = await request.json()
        if body.get("action") != "close":
            raise fail(400, "Неизвестное действие")
        if ticket["status"] not in tickets.OPEN_STATUSES:
            raise fail(400, "Тикет уже завершён")
        result = body.get("result")
        if result not in ("accepted", "declined"):
            raise fail(400, "Выберите итог: принят или отклонён")
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise fail(400, "Укажите причину")
        # Taking a ticket needs the author's consent in Discord; the panel can only finish it
        await tickets.close_ticket(self.bot, ticket, member, result, reason)
        status = ticket["status"]
        return web.json_response({"status": status})

    async def send_message(self, request: web.Request):
        """Message builder: send a new Components V2 message or edit one the bot sent earlier."""
        _, guild, member = await self.manageable(request)
        body = await request.json()
        channel = guild.get_channel(parse_id(body.get("channel")) or 0)
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            raise fail(400, "Выберите текстовый канал")
        if not channel.permissions_for(member).send_messages:
            raise fail(403, "У вас нет права писать в этот канал")
        try:
            view = message_builder.build_view(body.get("components"))
        except message_builder.BuildError as e:
            raise fail(400, str(e))
        mentions = (discord.AllowedMentions(everyone=False, users=True, roles=True)
                    if body.get("ping") else discord.AllowedMentions.none())

        edit = str(body.get("edit") or "").strip()
        if edit:
            match = re.search(r"(\d+)\D*$", edit)
            if not match:
                raise fail(400, "Вставьте ссылку на сообщение или его ID")
            try:
                message = await channel.fetch_message(int(match[1]))
            except discord.NotFound:
                raise fail(404, "Сообщение не найдено в выбранном канале")
            if message.author.id != self.bot.user.id:
                raise fail(400, "Редактировать можно только сообщения Vex")
            await message.edit(view=view, allowed_mentions=mentions)
        else:
            message = await channel.send(view=view, allowed_mentions=mentions)
        log.info("%s %s message %s in #%s (%s)", member, "edited" if edit else "sent", message.id, channel, guild.id)
        return web.json_response({"url": message.jump_url, "id": str(message.id)})


async def setup(bot: commands.Bot):
    await bot.add_cog(PanelAPI(bot))
