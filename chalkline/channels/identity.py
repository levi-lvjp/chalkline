import re
from collections.abc import Mapping


PHONE = re.compile(r"\+([1-9][0-9]{1,14})")
PHONE_JID = re.compile(r"([1-9][0-9]{1,14})(?::[0-9]+)?@s\.whatsapp\.net")
LID = re.compile(r"[0-9]+(?::[0-9]+)?@lid")


def dm_phone(source):
    """Get a phone identity from a direct chat, using the bridge's phone alias."""
    get = source.get if isinstance(source, Mapping) else lambda field: getattr(source, field, None)
    if get("chat_type") != "dm":
        return None
    sender, chat = get("user_id") or "", get("chat_id") or ""
    match = PHONE.fullmatch(sender) or PHONE_JID.fullmatch(sender)
    if not match:
        return None
    chat_match = PHONE.fullmatch(chat) or PHONE_JID.fullmatch(chat)
    if (chat_match and chat_match[1] == match[1]) or LID.fullmatch(chat):
        return f"+{match[1]}"
    return None


def unresolved_sender(source):
    """Return a stable LID for a direct message whose phone alias is unavailable."""
    if source.chat_type == "dm" and LID.fullmatch(source.user_id or "") and source.chat_id == source.user_id:
        return source.user_id
    return None
