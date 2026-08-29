"""Small Telegram MarkdownV2 helpers used by the standalone bot."""

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
