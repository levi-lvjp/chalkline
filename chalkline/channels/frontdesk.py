import asyncio
import hashlib
import logging
from datetime import timezone
from pathlib import Path

from .identity import dm_phone, unresolved_sender
from .text import customer_text


logger = logging.getLogger(__name__)
SKIP = {"action": "skip"}


def schedule_delivery(outbox_id):
    from .outbound import schedule_delivery as send

    send(outbox_id)


DAILY_MEDIA_CAP = 30


def _media(event):
    paths = event.media_urls or []
    types = event.media_types or []
    items = []
    for index, path in enumerate(paths):
        mime = types[index] if index < len(types) else ""
        kind = "photo" if mime.startswith("image/") else "voice" if mime.startswith("audio/") else "document"
        items.append({"kind": kind, "path": Path(path)})
    return items


def _within_media_cap(sender, media):
    """Keep at most DAILY_MEDIA_CAP stored attachments per customer per rolling 24 hours."""
    if not media:
        return media
    from datetime import timedelta

    from ..core import queries

    used = queries.media_count_since(sender, queries.clock_now() - timedelta(hours=24))
    return media[:max(0, DAILY_MEDIA_CAP - used)]


def _queue_replies(phone, capture, event, media):
    from ..core import queries, service

    job_id = capture["job_id"]
    owners = queries.settings().get("owner_phones") or []
    name = _display_name(event)
    photo_count = sum(item["kind"] == "photo" for item in media)
    voice_count = sum(item["kind"] == "voice" for item in media)
    summary = [event.text or ""]
    if photo_count:
        summary.append(f"{photo_count} photo(s)")
    if voice_count:
        summary.append(f"{voice_count} voice note(s)")
    preview = ", ".join(part for part in summary if part).replace("\n", " ")[:240]
    identity_note = "" if phone else "Phone unavailable. Owner review needed.\n"
    notice = customer_text(
        f'Enquiry {capture["job_number"]}. Customer-supplied data follows.\n'
        f'{identity_note}Name: {name[:80]}\nMessage: {preview}\nEnd of customer-supplied data.'
    )
    event_key = event.message_id or hashlib.sha256(repr((phone, event.text, media)).encode()).hexdigest()
    notice_row = None
    if owners:
        sender_key = hashlib.sha256((phone or unresolved_sender(event.source)).encode()).hexdigest()
        hour = queries.clock_now().astimezone(timezone.utc).strftime("%Y%m%d%H")
        notice_row = service.enqueue_owner_notice(owners[0], notice, f"enquiry-notice:{sender_key}:{hour}")
    ack_row = service.enqueue_message(
        "enquiry_ack", phone, "", f"enquiry-ack:{job_id}:{event_key}",
        job_id=job_id, standing_template="enquiry_ack",
    ) if phone else None
    return [row["id"] for row in (notice_row, ack_row) if row and row["status"] == "approved"]


def _display_name(event):
    name = " ".join(str(event.source.user_name or "").split())
    return customer_text(name)[:200] or "WhatsApp customer (display name unavailable)"


def _capture_customer(phone, event, *, capture_only=False):
    from ..core import service

    sender = phone or unresolved_sender(event.source)
    if not sender:
        return []
    capture_enquiry = service.capture_enquiry if phone else service.capture_unresolved_enquiry
    args = {"text": event.text or "", "message_id": event.message_id, "name": _display_name(event)}
    try:
        media = _within_media_cap(sender, _media(event))
        if len(media) < len(event.media_urls or []):
            args["text"] = (args["text"] + "\n[Attachment not stored: daily media limit reached.]").strip()
        capture = capture_enquiry(sender, media=media, **args)
    except Exception:
        logger.exception("WhatsApp capture failed with media. Trying text-only capture.")
        args["text"] = args["text"] or "WhatsApp attachment unavailable."
        args["name"] = None
        capture_enquiry(sender, **args)
        return []
    if capture_only or capture.get("duplicate"):
        return []
    return _queue_replies(phone, capture, event, media)


async def _route(event):
    source = event.source
    if getattr(source.platform, "value", source.platform) != "whatsapp":
        return None
    from ..core import queries

    # Linked-device echoes may carry the recipient's ID as their apparent sender.
    if (event.metadata or {}).get("whatsapp_from_owner"):
        return SKIP
    phone = dm_phone(source)
    if not phone and not unresolved_sender(source):
        return SKIP
    try:
        if phone and await asyncio.to_thread(queries.is_owner, phone):
            return None
    except Exception:
        logger.exception("WhatsApp owner verification unavailable. Capturing only.")
        await asyncio.to_thread(_capture_customer, phone, event, capture_only=True)
        return SKIP
    for outbox_id in await asyncio.to_thread(_capture_customer, phone, event):
        schedule_delivery(outbox_id)
    return SKIP


async def pre_gateway_dispatch(event=None, **_context):
    """Never let malformed ingress or an optional dependency fail open in Hermes."""
    try:
        return await _route(event)
    except (Exception, SystemExit):
        logger.exception("Front desk ingress failed. Attempting capture only.")
        try:
            phone = dm_phone(event.source)
            if phone or unresolved_sender(event.source):
                await asyncio.to_thread(_capture_customer, phone, event, capture_only=True)
        except (Exception, SystemExit):
            logger.exception("Capture-only fallback unavailable. Suppressing the agent turn.")
        return SKIP
