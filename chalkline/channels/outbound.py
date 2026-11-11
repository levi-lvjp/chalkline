import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from hermes_constants import get_hermes_home

from .. import SLUG
from ..core import queries, service
from .identity import PHONE


logger = logging.getLogger(__name__)
SEND_TIMEOUT = 45
MEDIA_SYNTAX = re.compile(r"MEDIA:|\[\[(?:audio_as_voice|as_document)\]\]", re.IGNORECASE)
_deliveries = set()


def _send_whatsapp(recipient, body, attachment_path=None):
    if not PHONE.fullmatch(recipient):
        return "failed", "Invalid WhatsApp recipient."
    if MEDIA_SYNTAX.search(body):
        return "failed", "Message text contains a reserved Hermes media directive."
    if attachment_path:
        path = Path(attachment_path).resolve()
        if not path.is_file() or path.suffix.lower() != ".pdf" or any(c in str(path) for c in '"\r\n'):
            return "failed", "PDF attachment is unavailable."
        body = f'{body}\nMEDIA:"{path.as_posix()}"'

    home = get_hermes_home().resolve()
    root = home.parent.parent if home.parent.name == "profiles" else home
    if home == root:
        profile = "default"
    elif home.parent == root / "profiles":
        profile = home.name
    else:
        return "failed", "Unable to resolve the active Hermes profile."
    env = {**os.environ, "HERMES_HOME": str(root), "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            ["hermes", "-p", profile, "send", "--to", f"whatsapp:{recipient}", "--json", "--file", "-"],
            input=body, capture_output=True, encoding="utf-8", errors="replace",
            env=env, timeout=SEND_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return "unknown", "Hermes send timed out."
    except OSError:
        return "failed", "Unable to launch the Hermes send CLI."

    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        return "unknown", "Hermes exited without a readable delivery result."
    if not isinstance(payload, dict):
        return "unknown", "Hermes returned an unexpected delivery result."
    if result.returncode == 0 and payload.get("success") is True and not payload.get("error"):
        return "sent", ""
    if payload.get("partial_success"):
        return "unknown", "Only part of the approved message was delivered."
    error = str(payload.get("error") or "").strip().lower()
    # The adapter catches HTTP timeouts and may stringify them as an empty exception.
    if "timeout" in error or "timed out" in error or error == "whatsapp send failed:":
        return "unknown", "The WhatsApp bridge timed out."
    if error or payload.get("skipped") or payload.get("success") is False:
        return "failed", "Hermes reported a delivery failure."
    return "unknown", "Hermes did not confirm delivery."


def deliver_outbox(outbox_id, *, now=None):
    """Claim an approved row and record the result of its only send attempt."""
    row = service.claim_outbox(outbox_id, now=now)
    if row is None:
        _deliver_notices(outbox_id, now)
        return None
    try:
        outcome, detail = _send_claimed(row)
    except Exception:
        logger.exception("WhatsApp delivery interrupted for outbox row %s", outbox_id)
        outcome, detail = "unknown", "Delivery stopped before the result could be recorded."
    service.finish_outbox(outbox_id, outcome, detail=detail, now=now)
    _deliver_notices(outbox_id, now)
    return outcome


def _deliver_notices(outbox_id, now):
    try:
        for notice in queries.delivery_notices(outbox_id):
            deliver_outbox(notice["id"], now=now)
    except Exception:
        logger.exception("Delivery result notice remains queued for outbox row %s.", outbox_id)


def _send_claimed(row):
    if not row.get("attachment_path"):
        return _send_whatsapp(row["recipient"], row["body"])
    source = Path(row["attachment_path"])
    scratch = get_hermes_home() / "cache" / "scratch"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=f"{SLUG}-", dir=scratch, ignore_cleanup_errors=True) as directory:
            staged = Path(directory) / source.name
            shutil.copyfile(source, staged)
            with staged.open("rb") as document:
                digest = hashlib.file_digest(document, "sha256").hexdigest()
            if digest != row["attachment_sha256"]:
                return "failed", "The approved PDF changed before delivery."
            return _send_whatsapp(row["recipient"], row["body"], staged)
    except OSError:
        return "failed", "The approved PDF could not be staged for delivery."


def schedule_delivery(outbox_id):
    """Send outside the gateway hook while retaining the active profile context."""
    async def send():
        try:
            await asyncio.to_thread(deliver_outbox, outbox_id)
        except Exception:
            logger.exception("Outbox delivery failed for row %s", outbox_id)

    task = asyncio.create_task(send())
    _deliveries.add(task)
    task.add_done_callback(_deliveries.discard)
