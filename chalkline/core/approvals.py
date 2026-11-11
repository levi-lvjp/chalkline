"""Canonical approval payloads and customer-message validation."""

import re
from hashlib import sha256
from pathlib import Path
from string import Formatter

from plugins.plugin_storage import plugin_data_dir

from .. import SLUG
from .files import file_digest
from .records import dumps
from .validation import TEMPLATES, text_field


MEDIA_SYNTAX = re.compile(r"MEDIA:|\[\[(?:audio_as_voice|as_document)\]\]", re.IGNORECASE)


def payload_hash(value: object) -> str:
    """Hash canonical JSON for exact-payload comparison."""
    return sha256(dumps(value).encode("utf-8")).hexdigest()


def owner_binding(settings: dict) -> dict:
    """Snapshot the authorized approval routes at proposal time."""
    return {
        "phones": sorted(settings["owner_phones"]),
        "telegram_user": settings["telegram_owner_user_id"],
        "telegram_chat": settings["telegram_owner_chat_id"],
    }


def template_text(kind: str, text: str) -> str:
    """Validate text and permit only simple, defined replacement fields."""
    if kind not in TEMPLATES:
        raise ValueError("Unknown standing template.")
    text = message_text(text)
    for _, name, spec, conversion in Formatter().parse(text):
        if name is not None and (name not in TEMPLATES[kind] or spec or conversion):
            raise ValueError(f"Invalid template field: {name!r}.")
    return text


def display_text(text: str) -> str:
    """Neutralize transport control tokens inside substituted data."""
    return MEDIA_SYNTAX.sub(lambda match: match.group().replace(":", chr(0x2236)).replace("[", chr(0xff3b)), text)

def message_text(text: str) -> str:
    """Validate text before passing it to the Hermes transport parser."""
    text = text_field(text, "message body", limit=4000)
    if MEDIA_SYNTAX.search(text):
        raise ValueError("Message body contains a reserved Hermes media directive.")
    return text


def attachment(path: str | Path | None) -> tuple[str | None, str | None]:
    """Bind a generated PDF from plugin storage to its exact bytes."""
    if path is None:
        return None, None
    resolved = Path(path).resolve()
    if resolved.suffix.lower() != ".pdf" or not resolved.is_relative_to(plugin_data_dir(SLUG).resolve()):
        raise ValueError("Use a generated PDF in the Chalkline data directory.")
    if any(character in str(resolved) for character in ('"', '\r', '\n')) or not resolved.is_file():
        raise ValueError("The PDF attachment is unavailable.")
    return str(resolved), file_digest(resolved)


def message_payload(row: dict) -> dict:
    """Return all content and targeting fields bound to a send approval."""
    return {key: row[key] for key in (
        "kind", "recipient", "body", "attachment_path", "attachment_sha256",
        "stage_key", "job_id", "related_id", "standing_hash",
    )}
