"""Steam inventory checker.

Someone posts a Steam profile link / SteamID in the guild's checker channel, the bot replies with
the Steam profile, FACEIT profile and the inventory value of CS2 / RUST / DOTA2 / TF2 in Steam Market prices.
"""
import asyncio
import json
import logging
import re
import time
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands

import config
import tickets
from config import GAMES
from storage import store

log = logging.getLogger("vex.checker")

NO_PINGS = discord.AllowedMentions.none()
# key in config.GAMES -> (Steam appid, inventory context)
INVENTORIES = {"cs2": (730, 2), "rust": (252490, 2), "dota2": (570, 2), "tf2": (440, 2)}
PRICE_TTL = 24 * 3600        # prices are re-fetched once a day
PRICE_GAP = 3.2              # seconds between Steam Market requests (Steam allows ~20 per minute)
INVENTORY_GAP = 1.5          # seconds between inventory requests
MAX_PRICE_LOOKUPS = 120      # new prices per check; the rest are fetched on the next check
USER_COOLDOWN = 20           # seconds between checks of one user
MAX_PARALLEL_CHECKS = 2
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VexBot inventory checker)"}
FACEIT_REGIONS = {"EU": "🇪🇺", "NA": "🇺🇸", "US": "🇺🇸", "SA": "🇧🇷", "OCE": "🇦🇺", "SEA": "🌏"}
STEAM_LINK = re.compile(r"steamcommunity\.com/(id|profiles)/|7656119\d{10}|STEAM_[0-5]:|\[U:1:", re.I)


class RateLimited(Exception):
    pass


def flag(country: str | None) -> str:
    if not country or len(country) != 2 or not country.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in country.upper())


def money(value: float) -> str:
    return f"{value:,.2f}$".replace(",", " ")


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    digits = re.sub(r"[^\d.,]", "", text).replace(",", "")
    try:
        return float(digits)
    except ValueError:
        return None


class PriceCache:
    """Steam Market prices: 'appid|market_hash_name' -> [price or None, fetched_at]."""

    def __init__(self, path):
        self.path = path
        self.data: dict[str, list] = {}
        self.dirty = False
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("Price cache is broken, starting empty")

    def get(self, appid: int, name: str) -> tuple[bool, float | None]:
        record = self.data.get(f"{appid}|{name}")
        if record and time.time() - record[1] < PRICE_TTL:
            return True, record[0]
        return False, None

    def set(self, appid: int, name: str, price: float | None) -> None:
        self.data[f"{appid}|{name}"] = [price, int(time.time())]
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        now = time.time()
        self.data = {k: v for k, v in self.data.items() if now - v[1] < PRICE_TTL * 7}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = False


class SteamClient:
    def __init__(self):
        self.http: aiohttp.ClientSession | None = None
        self.price_lock = asyncio.Lock()
        self.inventory_lock = asyncio.Lock()
        self.last_price = 0.0
        self.last_inventory = 0.0

    async def start(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25), headers=HEADERS)

    async def close(self):
        if self.http:
            await self.http.close()

    async def _json(self, url: str, **kw):
        async with self.http.get(url, **kw) as resp:
            if resp.status == 429:
                raise RateLimited()
            if resp.status != 200:
                return resp.status, None
            return 200, await resp.json(content_type=None)

    async def resolve(self, text: str) -> str | None:
        vanity = re.search(r"steamcommunity\.com/id/([\w.-]+)", text, re.I)
        if vanity and config.STEAM_API_KEY:
            try:
                _, data = await self._json("https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
                                           params={"key": config.STEAM_API_KEY, "vanityurl": vanity[1]})
                response = (data or {}).get("response", {})
                if response.get("success") == 1:
                    return response["steamid"]
            except (aiohttp.ClientError, asyncio.TimeoutError, RateLimited):
                pass
        return await tickets.resolve_steam64(text)

    async def summary(self, steam64: str) -> dict | None:
        if not config.STEAM_API_KEY:
            return None
        try:
            _, data = await self._json("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
                                       params={"key": config.STEAM_API_KEY, "steamids": steam64})
        except (aiohttp.ClientError, asyncio.TimeoutError, RateLimited):
            return None
        players = (data or {}).get("response", {}).get("players", [])
        return players[0] if players else None

    async def faceit(self, steam64: str) -> dict | None:
        """FACEIT player or {} when not found; None when there is no key or the API failed."""
        if not config.FACEIT_API_KEY:
            return None
        headers = {"Authorization": f"Bearer {config.FACEIT_API_KEY}"}
        try:
            for game in ("cs2", "csgo"):
                status, data = await self._json("https://open.faceit.com/data/v4/players",
                                                params={"game": game, "game_player_id": steam64}, headers=headers)
                if status == 200 and data:
                    return data
            return {}
        except (aiohttp.ClientError, asyncio.TimeoutError, RateLimited):
            return None

    @staticmethod
    def _parse_items(data: dict) -> list[tuple]:
        descriptions = {(str(d["classid"]), str(d.get("instanceid", "0"))): d for d in data.get("descriptions", [])}
        items = []
        for asset in data.get("assets", []):
            d = descriptions.get((str(asset["classid"]), str(asset.get("instanceid", "0"))))
            if d:
                items.append((d.get("market_hash_name") or d.get("name", "?"), int(asset.get("amount", 1)),
                              d.get("tradable") in (1, True), d.get("marketable") in (1, True)))
        return items

    async def _inventory_page(self, url: str, params: dict) -> tuple[int, dict | None]:
        """One request, spaced out and retried when Steam answers 429."""
        for delay in (0, 8, 20):
            if delay:
                await asyncio.sleep(delay)
            async with self.inventory_lock:
                wait = self.last_inventory + INVENTORY_GAP - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.last_inventory = time.monotonic()
                try:
                    return await self._json(url, params=params)
                except RateLimited:
                    continue
        raise RateLimited()

    async def inventory(self, steam64: str, appid: int, context: int) -> dict:
        """{"status": ok|private|error|ratelimit, "items": [(name, amount, tradable, marketable)]}"""
        if config.STEAM_API_KEY:
            # Official Web API: limits are per key, not per server IP
            result = await self._inventory_api(steam64, appid, context)
            if result is not None:
                return result
        return await self._inventory_community(steam64, appid, context)

    async def _inventory_api(self, steam64: str, appid: int, context: int) -> dict | None:
        items, start = [], None
        url = "https://api.steampowered.com/IEconService/GetInventoryItemsWithDescriptions/v1/"
        for _ in range(5):
            params = {"key": config.STEAM_API_KEY, "steamid": steam64, "appid": appid, "contextid": context,
                      "get_descriptions": "true", "count": 2000}
            if start:
                params["start_assetid"] = start
            try:
                status, data = await self._inventory_page(url, params)
            except (RateLimited, aiohttp.ClientError, asyncio.TimeoutError):
                return None
            if status != 200 or data is None:
                return None  # private inventory, bad key, ... -> let the community endpoint decide
            response = data.get("response", {})
            items += self._parse_items(response)
            if not response.get("more_items") or not response.get("last_assetid"):
                break
            start = response["last_assetid"]
        return {"status": "ok", "items": items}

    async def _inventory_community(self, steam64: str, appid: int, context: int) -> dict:
        items, start = [], None
        url = f"https://steamcommunity.com/inventory/{steam64}/{appid}/{context}"
        for _ in range(5):  # up to 10 000 items
            params = {"l": "english", "count": 2000}
            if start:
                params["start_assetid"] = start
            try:
                status, data = await self._inventory_page(url, params)
            except RateLimited:
                return {"status": "ratelimit", "items": items}
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return {"status": "error", "items": items}
            if status == 403:
                return {"status": "private", "items": []}
            if status != 200:
                # Steam answers 500 / null for some empty inventories
                return {"status": "ok" if status in (500, 400) and not items else "error", "items": items}
            if not data:
                break
            if data.get("success") not in (1, True):
                return {"status": "error", "items": items}
            items += self._parse_items(data)
            if not data.get("more_items") or not data.get("last_assetid"):
                break
            start = data["last_assetid"]
        return {"status": "ok", "items": items}

    async def price(self, appid: int, name: str) -> float | None:
        async with self.price_lock:
            wait = self.last_price + PRICE_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_price = time.monotonic()
            _, data = await self._json(
                f"https://steamcommunity.com/market/priceoverview/?appid={appid}&currency=1"
                f"&market_hash_name={quote(name, safe='')}")
        if not data or not data.get("success"):
            return None
        return parse_price(data.get("lowest_price") or data.get("median_price"))


class LztClient:
    """lzt.market GET /steam-value: inventory value in LZT prices (20 requests per minute)."""
    GAP = 3.1

    def __init__(self, steam: SteamClient):
        self.steam = steam
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def value(self, steam64: str, appid: int) -> dict:
        """{"status": ok|private|error|ratelimit, "value", "tradable", "nontradable", "message"}"""
        async with self.lock:
            wait = self.last + self.GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.last = time.monotonic()
            try:
                async with self.steam.http.get(
                    f"{config.LZT_API_URL}/steam-value",
                    params={"link": steam64, "app_id": appid, "currency": "usd"},
                    headers={"Authorization": f"Bearer {config.LZT_API_TOKEN}", "Accept": "application/json"},
                ) as resp:
                    status = resp.status
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError:
                        data = None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return {"status": "error", "message": "LZT не отвечает"}

        if status == 429:
            return {"status": "ratelimit"}
        if status in (401, 403):
            return {"status": "error", "message": "LZT: неверный токен или нет права market"}
        payload = (data or {}).get("data") if isinstance(data, dict) else None
        if status != 200 or not isinstance(payload, dict):
            errors = (data or {}).get("errors") if isinstance(data, dict) else None
            message = str(errors[0]) if isinstance(errors, list) and errors else f"LZT вернул {status}"
            private = any(w in message.lower() for w in ("private", "скрыт", "закрыт", "приват"))
            return {"status": "private" if private else "error", "message": message}

        items = payload.get("items") or {}
        items = list(items.values()) if isinstance(items, dict) else list(items)
        count = lambda i: int(i.get("count") or 1)  # noqa: E731
        return {
            "status": "ok",
            "value": float(payload.get("totalValue") or 0),
            "tradable": sum(count(i) for i in items if i.get("tradable")),
            "nontradable": sum(count(i) for i in items if not i.get("tradable")),
        }


# ---------- views ----------

def status_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=0xF0B232))
    return view


def game_emoji(guild_id: int, key: str) -> str:
    return store.settings(guild_id)["games"].get(key, {}).get("emoji") or GAMES[key]["emoji"]


def price_text(g: dict, mode: str) -> str:
    """'111.62$' or, when both sources are shown, '111.62$ (Steam) · 98.40$ (LZT)'."""
    if mode == "both":
        parts = []
        if g.get("steam") is not None:
            parts.append(f"{money(g['steam'])} (Steam)")
        if g.get("lzt") is not None:
            parts.append(f"{money(g['lzt'])} (LZT)")
        return " · ".join(parts) or "—"
    value = g.get("lzt") if mode == "lzt" else g.get("steam")
    return money(value or 0)


def result_view(guild_id: int, steam64: str, summary: dict | None, faceit: dict | None,
                games: dict[str, dict], unpriced: int, requested_by: str, mode: str = "steam") -> discord.ui.LayoutView:
    lines = []
    if summary:
        country = summary.get("loccountrycode")
        name = discord.utils.escape_markdown(summary.get("personaname", "профиль"))
        line = f"▪️ **Steam Profile:** [{name}]({summary.get('profileurl')})"
        if country:
            line += f" `{country}` {flag(country)}"
        if summary.get("communityvisibilitystate") != 3:
            line += " · 🔒 профиль скрыт"
        lines.append(line)
    else:
        lines.append(f"▪️ **Steam Profile:** [открыть](https://steamcommunity.com/profiles/{steam64})")
    lines.append(f"▪️ **Steam ID64:** `{steam64}`")
    if faceit is not None:
        if faceit:
            region = (faceit.get("games", {}).get("cs2") or faceit.get("games", {}).get("csgo") or {})
            url = (faceit.get("faceit_url") or "").replace("{lang}", "ru")
            line = f"▪️ **FACEIT Profile:** [{discord.utils.escape_markdown(faceit.get('nickname', '?'))}]({url})"
            if region.get("region"):
                line += f" `{region['region']}` {FACEIT_REGIONS.get(region['region'], '')}"
            if region.get("skill_level"):
                line += f" · lvl {region['skill_level']} · {region.get('faceit_elo', '?')} elo"
            lines.append(line)
        else:
            lines.append("▪️ **FACEIT Profile:** не найден")

    steam_total = sum(g.get("steam") or 0 for g in games.values())
    lzt_total = sum(g.get("lzt") or 0 for g in games.values())
    if mode == "both":
        rows = [f"▪️ **Общая стоимость инвентаря:** {money(steam_total)} (Steam) · {money(lzt_total)} (LZT)"]
    elif mode == "lzt":
        rows = [f"▪️ **Общая стоимость инвентаря: {money(lzt_total)}** (LZT PRICE)"]
    else:
        rows = [f"▪️ **Общая стоимость инвентаря: {money(steam_total)}** (STEAM PRICE)"]
    for key, g in games.items():
        head = f"{game_emoji(guild_id, key)} {GAMES[key]['name']}:"
        if g["status"] == "private":
            rows.append(f"{head} 🔒 инвентарь скрыт")
        elif g["status"] == "ratelimit":
            rows.append(f"{head} ⏳ слишком много запросов, попробуйте позже")
        elif g["status"] == "error":
            rows.append(f"{head} ⚠️ {g.get('message') or 'не удалось загрузить'}")
        else:
            rows.append(f"{head} {price_text(g, mode)} (tradable {g['tradable']} / nontradable {g['nontradable']})")

    source = {"lzt": "Цены LZT Market (USD)", "steam": "Цены Steam Community Market (lowest price, USD)",
              "both": "Цены Steam Community Market и LZT Market (USD)"}[mode]
    footer = f"-# {source} · запросил {requested_by}"
    if unpriced:
        footer += f"\n-# Без цены пока {unpriced} предм. — Steam отдаёт цены медленно, повторная проверка их досчитает."

    container = discord.ui.Container(accent_color=0x5865F2)
    container.add_item(discord.ui.TextDisplay("\n".join(lines)))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay("\n".join(rows)))
    container.add_item(discord.ui.TextDisplay(footer))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


# ---------- cog ----------

class Checker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.steam = SteamClient()
        self.lzt = LztClient(self.steam)
        self.prices = PriceCache(config.PRICES_PATH)
        self.cooldowns: dict[int, float] = {}
        self.slots = asyncio.Semaphore(MAX_PARALLEL_CHECKS)

    async def cog_load(self):
        await self.steam.start()
        if not config.STEAM_API_KEY:
            log.warning("STEAM_API_KEY is not set: the checker will show inventories without profile names/countries")

    async def cog_unload(self):
        self.prices.save()
        await self.steam.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if message.channel.id != store.settings(message.guild.id)["checker_channel"]:
            return
        if not STEAM_LINK.search(message.content):
            await message.reply(view=status_view(
                "Пришлите ссылку на Steam профиль или SteamID64 — например `https://steamcommunity.com/profiles/7656…`"),
                mention_author=False, delete_after=15)
            return
        left = self.cooldowns.get(message.author.id, 0) + USER_COOLDOWN - time.monotonic()
        if left > 0:
            await message.reply(view=status_view(f"Подождите {int(left) + 1} сек. перед следующей проверкой."),
                                mention_author=False, delete_after=10)
            return
        self.cooldowns[message.author.id] = time.monotonic()
        try:
            await self.run_check(message)
        except Exception:
            log.exception("Inventory check failed")

    async def run_check(self, message: discord.Message):
        reply = await message.reply(view=status_view("⏳ Ищу профиль…"), mention_author=False)
        steam64 = await self.steam.resolve(message.content)
        if not steam64:
            await reply.edit(view=status_view("❌ Не удалось определить SteamID. Проверьте ссылку."))
            return

        source = store.settings(message.guild.id)["checker_source"]
        use_lzt = source in ("lzt", "both") and bool(config.LZT_API_TOKEN)
        use_steam = source in ("steam", "both") or not use_lzt

        async with self.slots:
            await reply.edit(view=status_view("⏳ Загружаю профиль…"))
            summary, faceit = await asyncio.gather(self.steam.summary(steam64), self.steam.faceit(steam64))

            lzt: dict[str, dict] = {}
            if use_lzt:
                for n, (key, (appid, _)) in enumerate(INVENTORIES.items(), 1):
                    await reply.edit(view=status_view(f"⏳ Считаю инвентарь по ценам LZT: {GAMES[key]['name']} ({n}/4)"))
                    lzt[key] = await self.lzt.value(steam64, appid)
                if source == "lzt" and all(r["status"] in ("error", "ratelimit") for r in lzt.values()):
                    log.warning("LZT failed for every game (%s), falling back to Steam prices",
                                next(iter(lzt.values())).get("message"))
                    use_steam, use_lzt, lzt = True, False, {}

            inventories: dict[str, dict] = {}
            unpriced = 0
            if use_steam:
                await reply.edit(view=status_view("⏳ Загружаю инвентари Steam…"))
                inventories = {key: await self.steam.inventory(steam64, appid, ctx)
                               for key, (appid, ctx) in INVENTORIES.items()}
                await self.fetch_prices(reply, inventories)

        mode = "both" if (use_lzt and use_steam) else ("lzt" if use_lzt else "steam")
        games = {}
        for key in INVENTORIES:
            inv, lz = inventories.get(key), lzt.get(key)
            g = {"status": "error", "tradable": 0, "nontradable": 0, "steam": None, "lzt": None}
            if lz and lz["status"] == "ok":
                g.update(status="ok", lzt=lz["value"], tradable=lz["tradable"], nontradable=lz["nontradable"])
            if inv and inv["status"] == "ok":
                steam_value, missing = self.steam_value(INVENTORIES[key][0], inv["items"])
                unpriced += missing
                g.update(status="ok", steam=steam_value,
                         tradable=sum(i[1] for i in inv["items"] if i[2]),
                         nontradable=sum(i[1] for i in inv["items"] if not i[2]))
            if g["status"] != "ok":
                failed = lz if lz and (not inv or lz["status"] == "private") else inv or lz or {}
                g["status"] = failed.get("status", "error")
                g["message"] = failed.get("message")
            games[key] = g
        await reply.edit(view=result_view(message.guild.id, steam64, summary, faceit, games, unpriced,
                                          message.author.name, mode), allowed_mentions=NO_PINGS)

    async def fetch_prices(self, reply: discord.Message, inventories: dict[str, dict]) -> None:
        """Steam Market prices: cached ones are free, new ones go through the rate-limited queue."""
        needed = []
        for key, inv in inventories.items():
            appid = INVENTORIES[key][0]
            for name in {i[0] for i in inv["items"] if i[3]}:
                if not self.prices.get(appid, name)[0]:
                    needed.append((appid, name))
        lookups = needed[:MAX_PRICE_LOOKUPS]
        for n, (appid, name) in enumerate(lookups, 1):
            if n == 1 or n % 8 == 0:
                eta = int((len(lookups) - n) * PRICE_GAP)
                await reply.edit(view=status_view(f"⏳ Узнаю цены Steam Market: {n}/{len(lookups)} · ещё ~{eta} сек."))
            try:
                self.prices.set(appid, name, await self.steam.price(appid, name))
            except RateLimited:
                log.warning("Steam Market rate limit hit after %s prices", n - 1)
                break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
        self.prices.save()

    def steam_value(self, appid: int, items: list[tuple]) -> tuple[float, int]:
        """(value in Steam prices, number of marketable items without a known price yet)"""
        value, missing = 0.0, 0
        for name, amount, _tradable, marketable in items:
            if not marketable:
                continue
            known, price = self.prices.get(appid, name)
            if not known:
                missing += amount
            elif price:
                value += price * amount
        return value, missing


async def setup(bot: commands.Bot):
    await bot.add_cog(Checker(bot))
