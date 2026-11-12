"""Turns the dashboard's message-builder JSON into a Components V2 LayoutView."""
import re

import discord

MAX_COMPONENTS = 40   # Discord limit for all components of one message, nested ones included
MAX_TEXT = 4000       # Discord limit for all text of one message
URL_RE = re.compile(r"^https?://\S+$")


class BuildError(ValueError):
    pass


class Builder:
    def __init__(self):
        self.count = 0
        self.text = 0

    def bump(self, n: int = 1):
        self.count += n
        if self.count > MAX_COMPONENTS:
            raise BuildError(f"Слишком много компонентов: максимум {MAX_COMPONENTS}")

    def text_display(self, content) -> discord.ui.TextDisplay:
        content = str(content or "").strip()
        if not content:
            raise BuildError("Есть пустой текстовый блок")
        self.text += len(content)
        if self.text > MAX_TEXT:
            raise BuildError(f"Слишком много текста: максимум {MAX_TEXT} символов на сообщение")
        self.bump()
        return discord.ui.TextDisplay(content)

    @staticmethod
    def url(value, what: str) -> str:
        value = str(value or "").strip()
        if not URL_RE.match(value) or len(value) > 2000:
            raise BuildError(f"{what}: нужна ссылка, начинающаяся с http:// или https://")
        return value

    def link_button(self, data: dict) -> discord.ui.Button:
        label = str(data.get("label") or "").strip()[:80]
        emoji = str(data.get("emoji") or "").strip() or None
        if not label and not emoji:
            raise BuildError("У кнопки должен быть текст или эмодзи")
        self.bump()
        try:
            emoji_obj = discord.PartialEmoji.from_str(emoji) if emoji else None
        except Exception:
            raise BuildError(f"Неверный эмодзи: {emoji}")
        return discord.ui.Button(style=discord.ButtonStyle.link, label=label or None,
                                 emoji=emoji_obj, url=self.url(data.get("url"), "Кнопка"))

    def item(self, block: dict, nested: bool) -> discord.ui.Item:
        kind = block.get("type")
        if kind == "text":
            return self.text_display(block.get("content"))

        if kind == "separator":
            self.bump()
            spacing = discord.SeparatorSpacing.large if block.get("spacing") == "large" else discord.SeparatorSpacing.small
            return discord.ui.Separator(visible=bool(block.get("divider", True)), spacing=spacing)

        if kind == "section":
            self.bump()
            text = self.text_display(block.get("content"))
            acc = block.get("accessory") or {}
            if acc.get("type") == "thumbnail":
                self.bump()
                accessory = discord.ui.Thumbnail(self.url(acc.get("url"), "Картинка секции"))
            else:
                accessory = self.link_button(acc)
            return discord.ui.Section(text, accessory=accessory)

        if kind == "media":
            items = block.get("items") or []
            if not 1 <= len(items) <= 10:
                raise BuildError("В медиа-галерее должно быть от 1 до 10 картинок")
            self.bump(1 + len(items))
            return discord.ui.MediaGallery(*(
                discord.MediaGalleryItem(self.url(i.get("url"), "Картинка"),
                                         description=(str(i.get("description") or "")[:1024] or None))
                for i in items
            ))

        if kind == "buttons":
            buttons = block.get("buttons") or []
            if not 1 <= len(buttons) <= 5:
                raise BuildError("В ряду должно быть от 1 до 5 кнопок")
            self.bump()
            return discord.ui.ActionRow(*(self.link_button(b) for b in buttons))

        if kind == "container":
            if nested:
                raise BuildError("Контейнер нельзя вложить в контейнер")
            children = block.get("children") or []
            if not children:
                raise BuildError("Контейнер пустой — добавьте в него блоки")
            self.bump()
            accent = block.get("accent")
            color = None
            if accent:
                if not re.fullmatch(r"#?[0-9a-fA-F]{6}", str(accent)):
                    raise BuildError("Неверный цвет контейнера")
                color = int(str(accent).lstrip("#"), 16)
            return discord.ui.Container(*(self.item(c, True) for c in children), accent_color=color,
                                        spoiler=bool(block.get("spoiler")))

        raise BuildError(f"Неизвестный блок: {kind}")


def build_view(components) -> discord.ui.LayoutView:
    if not isinstance(components, list) or not components:
        raise BuildError("Сообщение пустое — добавьте хотя бы один блок")
    builder = Builder()
    view = discord.ui.LayoutView(timeout=None)
    for block in components:
        if not isinstance(block, dict):
            raise BuildError("Неверный формат блока")
        view.add_item(builder.item(block, False))
    return view
