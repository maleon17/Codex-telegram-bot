"""Small Telegram formatting helpers used by the standalone bot."""

import re


_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#\+\-=|{}.!\\])")


def escape_mdv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


def strip_mdv2(text: str) -> str:
    """Remove MarkdownV2 escapes and simple inline markup."""
    cleaned = re.sub(r"\\([_*\[\]()~`>#\+\-=|{}.!\\])", r"\1", text)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", cleaned)
    cleaned = re.sub(r"~([^~]+)~", r"\1", cleaned)
    return re.sub(r"\|\|([^|]+)\|\|", r"\1", cleaned)


def _richtext_to_md(rich_text):
    if rich_text is None:
        return ""
    if isinstance(rich_text, str):
        return rich_text
    if isinstance(rich_text, list):
        return "".join(_richtext_to_md(item) for item in rich_text)
    if not isinstance(rich_text, dict):
        return str(rich_text)

    kind = rich_text.get("type")
    inner = _richtext_to_md(rich_text.get("text"))
    if kind == "bold":
        return f"**{inner}**"
    if kind == "italic":
        return f"_{inner}_"
    if kind == "underline":
        return f"__{inner}__"
    if kind == "strikethrough":
        return f"~~{inner}~~"
    if kind == "spoiler":
        return f"||{inner}||"
    if kind == "marked":
        return f"=={inner}=="
    if kind == "code":
        return f"`{inner}`"
    if kind == "superscript":
        return f"^{inner}^"
    if kind == "custom_emoji":
        return rich_text.get("alternative_text", "")
    if kind == "mathematical_expression":
        return f"${rich_text.get('expression', '')}$"
    if kind == "url":
        return f"[{inner}]({rich_text.get('url', '')})"
    if kind == "email_address":
        return f"[{inner}](mailto:{rich_text.get('email_address', '')})"
    if kind == "mention":
        return f"@{rich_text.get('username') or inner}"
    if kind == "hashtag":
        return f"#{rich_text.get('hashtag') or inner}"
    if kind == "cashtag":
        return f"${rich_text.get('cashtag') or inner}"
    if kind == "bot_command":
        return f"/{rich_text.get('bot_command') or inner}"
    return inner


def _rich_table_to_md(block):
    rows = block.get("cells") or []
    if not rows:
        return ""
    markdown_rows = []
    for row in rows:
        cells = []
        for cell in row:
            text = _richtext_to_md((cell or {}).get("text")) if cell else ""
            cells.append(text.replace("|", "\\|").replace("\n", " "))
        markdown_rows.append(cells)
    width = max(len(row) for row in markdown_rows)
    header, *rest = markdown_rows
    header += [""] * (width - len(header))
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in range(width)) + "|"]
    for row in rest:
        row += [""] * (width - len(row))
        lines.append("| " + " | ".join(row) + " |")
    result = "\n".join(lines)
    caption = block.get("caption")
    if caption:
        result = f"**{_richtext_to_md(caption)}**\n\n{result}"
    return result


def _rich_block_to_md(block):
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind == "paragraph":
        return _richtext_to_md(block.get("text"))
    if kind == "heading":
        level = max(1, min(6, block.get("size") or 3))
        return f"{'#' * level} {_richtext_to_md(block.get('text'))}"
    if kind == "pre":
        return f"```{block.get('language', '')}\n{_richtext_to_md(block.get('text'))}\n```"
    if kind == "footer":
        return f"_{_richtext_to_md(block.get('text'))}_"
    if kind == "divider":
        return "---"
    if kind == "mathematical_expression":
        return f"$$\n{block.get('expression', '')}\n$$"
    if kind == "anchor":
        return ""
    if kind == "list":
        lines = []
        for item in block.get("items", []):
            content = " ".join(
                _rich_block_to_md(child) for child in item.get("blocks", []) if child
            )
            if item.get("has_checkbox"):
                prefix = "- [x] " if item.get("is_checked") else "- [ ] "
            else:
                label = item.get("label") or "-"
                prefix = f"{label} " if label == "-" else f"{label}. "
            lines.append(f"{prefix}{content}")
        return "\n".join(lines)
    if kind in ("blockquote", "pullquote"):
        if kind == "blockquote":
            content = "\n".join(
                _rich_block_to_md(child) for child in block.get("blocks", []) if child
            )
        else:
            content = _richtext_to_md(block.get("text"))
        quoted = "\n".join(f"> {line}" for line in content.splitlines()) if content else ">"
        credit = block.get("credit")
        if credit:
            quoted += f"\n> — {_richtext_to_md(credit)}"
        return quoted
    if kind == "table":
        return _rich_table_to_md(block)
    if kind == "details":
        summary = _richtext_to_md(block.get("summary"))
        content = "\n".join(
            _rich_block_to_md(child) for child in block.get("blocks", []) if child
        )
        return f"<details>\n<summary>{summary}</summary>\n\n{content}\n</details>"
    if kind in ("collage", "slideshow", "animation", "audio", "photo", "video", "voice_note"):
        caption = block.get("caption")
        caption_text = _richtext_to_md(caption.get("text")) if isinstance(caption, dict) else ""
        return f"[{kind}{': ' + caption_text if caption_text else ''}]"
    return ""


def rich_message_to_markdown(rich_message):
    """Convert Telegram's rich_message extension into readable prompt text."""
    if not isinstance(rich_message, dict):
        return ""
    try:
        # Forwarded rich messages may retain the original markdown while
        # omitting structured blocks. Prefer it when present, then fall back
        # to blocks.
        for field in ("markdown", "text"):
            value = rich_message.get(field)
            if isinstance(value, str) and value.strip():
                return value
        blocks = rich_message.get("blocks") or []
        if isinstance(blocks, dict):
            blocks = [blocks]
        return "\n\n".join(
            result for result in (_rich_block_to_md(block) for block in blocks) if result
        )
    except Exception:
        # A malformed/unknown rich block must not make the whole Telegram
        # update disappear. The visible markdown/text fallback is still
        # useful when a client included one.
        return str(rich_message.get("markdown") or rich_message.get("text") or "")
