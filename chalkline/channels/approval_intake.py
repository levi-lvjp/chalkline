import asyncio
import json
import logging
import re
from pathlib import Path

from .. import SLUG
from ..core import queries, service
from .identity import dm_phone
from .outbound import schedule_delivery
from .text import customer_text


logger = logging.getLogger(__name__)
CODE = r"A[0-9]+"
WHATSAPP_APPROVAL = re.compile(rf"(APPROVE|REJECT)[ \t]+({CODE})", re.IGNORECASE | re.ASCII)
TELEGRAM_APPROVAL = re.compile(rf"\A{re.escape(SLUG)}:(approve|reject):({CODE})\Z")


async def _resolve(action, code, actor):
    try:
        resolve = service.approve if action == "approve" else service.reject
        result = await asyncio.to_thread(resolve, code, actor=actor)
        status = result["status"]
        if status == "approved" and result.get("outbox_id") is not None:
            schedule_delivery(result["outbox_id"])
        summary = result.get("summary")
        label = f"{code} · {customer_text(summary)}" if summary else code
        return f"{label} · {status.replace('_', ' ')}"
    except ValueError as exc:
        return str(exc)
    except (Exception, SystemExit):
        logger.exception("Approval intake failed for %s", code)
        return f"{code} · unable to resolve approval."


async def post_gateway_admission(platform, source, text, **_context):
    match = WHATSAPP_APPROVAL.fullmatch(text.strip()) if platform == "whatsapp" else None
    if not match:
        return None
    try:
        phone = dm_phone(source)
        if not phone or not await asyncio.to_thread(queries.is_owner, phone):
            return {"action": "handled"}
    except (Exception, SystemExit):
        logger.exception("WhatsApp approval identity verification failed.")
        return {"action": "handled"}
    action, code = match.groups()
    return {"action": "handled", "reply": await _resolve(action.lower(), code.upper(), phone)}


def _telegram_owner(update):
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat or chat.type != "private" or user.is_bot:
        return None
    settings = queries.settings()
    user_id, chat_id = str(user.id), str(chat.id)
    if user_id == str(settings.get("telegram_owner_user_id")) and chat_id == str(settings.get("telegram_owner_chat_id")):
        return f"telegram:{user_id}:{chat_id}"
    return None


async def telegram_callback(update, _context):
    query = update.callback_query
    match = TELEGRAM_APPROVAL.fullmatch(query.data or "")
    try:
        actor = await asyncio.to_thread(_telegram_owner, update)
    except Exception:
        logger.exception("Telegram approval identity verification failed.")
        actor = None
    if not match or not actor:
        await query.answer()
        return
    await query.answer()
    result = await _resolve(*match.groups(), actor)
    await query.edit_message_reply_markup(reply_markup=None)
    await update.effective_message.reply_text(result)


def telegram_factory(application, _adapter):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import CallbackQueryHandler, CommandHandler

    async def pending(update, _context):
        if not await asyncio.to_thread(_telegram_owner, update):
            return
        rows = await asyncio.to_thread(queries.pending_approvals)
        if not rows:
            await update.effective_message.reply_text("No pending Chalkline approvals.")
        for row in rows:
            code = row["code"]
            if not re.fullmatch(CODE, code):
                continue
            keyboard = [[
                InlineKeyboardButton("Approve", callback_data=f"{SLUG}:approve:{code}"),
                InlineKeyboardButton("Reject", callback_data=f"{SLUG}:reject:{code}"),
            ]]
            summary = row.get("summary") or row.get("kind") or "Pending approval"
            lines = [f"{code} · {summary}"]
            if row.get("recipient"):
                lines.append(row["recipient"])
            if row.get("attachment_path"):
                lines.append(f"PDF · {Path(row['attachment_path']).name}")
            if row.get("body"):
                lines.extend(("", row["body"]))
            if row.get("details"):
                lines.extend(("", json.dumps(row["details"], ensure_ascii=False, sort_keys=True, indent=2)))
            preview = "\n".join(lines)
            # Telegram limits messages by UTF-16 length, so allow up to two units per character.
            chunks = [preview[i:i + 2000] for i in range(0, len(preview), 2000)]
            for chunk in chunks[:-1]:
                await update.effective_message.reply_text(chunk, parse_mode=None)
            await update.effective_message.reply_text(chunks[-1], parse_mode=None,
                                                       reply_markup=InlineKeyboardMarkup(keyboard))

    application.add_handler(CallbackQueryHandler(telegram_callback, pattern=TELEGRAM_APPROVAL))
    application.add_handler(CommandHandler("office_pending", pending))
