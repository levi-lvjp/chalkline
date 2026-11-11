"""Transactional office writes. Channels provide verified transport identity.

Actor is "cli", an authenticated "dashboard" route, an E.164 WhatsApp number,
or "telegram:<user_id>:<chat_id>".
Callers must derive it from trusted metadata.
"""

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from sqlite3 import Connection
from typing import Iterable, NotRequired, TypedDict
from zoneinfo import ZoneInfo

from . import money
from .approvals import display_text, attachment, message_payload, message_text, owner_binding, payload_hash, template_text
from .files import copy_source, file_digest
from .numbering import allocate
from .pricing import price_lines
from .records import date_string, dumps, event, get_settings, phone_number, require_owner, row_for, timestamp
from .schema import connect
from .states import transition
from .validation import TEMPLATES, parse_instant, setup_values, text_field


class Media(TypedDict):
    kind: str
    path: str | Path
    text: NotRequired[str]


def configure(changes: dict, *, actor: str) -> dict:
    """Validate and persist owner-entered setup settings."""
    if actor != "cli":
        raise PermissionError("Run setup in the Hermes CLI.")
    with connect(write=True) as db:
        current = get_settings(db)
        updated = setup_values(current, changes)
        updated.setdefault("setup_at", timestamp())
        if updated["currency"] != current["currency"] and db.execute(
            """SELECT 1 FROM quotes UNION ALL SELECT 1 FROM invoices
               UNION ALL SELECT 1 FROM expenses UNION ALL SELECT 1 FROM variations
               UNION ALL SELECT 1 FROM jobs WHERE completion_lines IS NOT NULL LIMIT 1"""
        ).fetchone():
            raise ValueError("Business currency is locked once amounts are recorded.")
        if updated != current:
            db.execute("INSERT OR REPLACE INTO settings(id,data) VALUES (1,?)", (dumps(updated),))
            event(db, "configured", "settings", 1, actor=actor)
        return updated


def _customer(db: Connection, name: str, phone: str | None, address: str, notes: str, actor: str) -> dict:
    if phone:
        existing = db.execute("SELECT * FROM customers WHERE phone = ?", (phone_number(phone),)).fetchone()
        if existing:
            return dict(existing)
    customer_id = db.execute(
        "INSERT INTO customers(name,phone,address,notes) VALUES (?,?,?,?)",
        (text_field(name, "customer name", limit=200), phone, text_field(address, "address", optional=True),
         text_field(notes, "notes", optional=True)),
    ).lastrowid
    event(db, "created", "customer", customer_id, actor=actor)
    return row_for(db, "customers", customer_id)


def save_customer(
    *, actor: str, name: str | None = None, phone: str | None = None,
    address: str | None = None, notes: str | None = None, customer_id: int | None = None,
) -> dict:
    """Find or create a customer by phone, or edit the customer with an explicit ID."""
    with connect(write=True) as db:
        require_owner(db, actor)
        if customer_id is None:
            return _customer(db, name, phone, address or "", notes or "", actor)
        customer = row_for(db, "customers", customer_id)
        changes = {
            "name": text_field(name, "name", limit=200) if name is not None else customer["name"],
            "phone": phone_number(phone) if phone is not None else customer["phone"],
            "address": text_field(address, "address", optional=True) if address is not None else customer["address"],
            "notes": text_field(notes, "notes", optional=True) if notes is not None else customer["notes"],
        }
        if any(customer[key] != value for key, value in changes.items()):
            if changes["phone"] and db.execute(
                "SELECT 1 FROM customers WHERE phone = ? AND id <> ?", (changes["phone"], customer_id),
            ).fetchone():
                raise ValueError("This phone belongs to another customer.")
            db.execute("UPDATE customers SET name=?, phone=?, address=?, notes=? WHERE id=?",
                       (*changes.values(), customer_id))
            event(db, "updated", "customer", customer_id, actor=actor)
        return customer | changes


def _create_job(db: Connection, customer: dict, description: str, address: str, actor: str) -> dict:
    stamp = timestamp()
    number = allocate(db, "J")
    job_id = db.execute(
        """INSERT INTO jobs(number,customer_id,site_address,description,state,created_at,updated_at)
           VALUES (?,?,?,?,'enquiry',?,?)""",
        (number, customer["id"], address or customer["address"], description, stamp, stamp),
    ).lastrowid
    event(db, "created", "job", job_id, actor=actor, number=number)
    return row_for(db, "jobs", job_id)


def _attach(
    db: Connection, job_id: int, kind: str, sender: str, text: str,
    *, message_id: str | None = None, path: str | Path | None = None,
    copied: tuple[str, str] | None = None,
) -> int:
    if kind not in ("text", "photo", "voice", "receipt", "document"):
        raise ValueError("Unsupported evidence kind.")
    text = text_field(text, "evidence text", optional=True, limit=40000)
    file_path, digest = copied or (copy_source(path) if path else (None, None))
    if digest:
        row = db.execute("SELECT id FROM sources WHERE job_id = ? AND sha256 = ?", (job_id, digest)).fetchone()
        if row:
            return row["id"]
    if message_id:
        row = db.execute("SELECT id, job_id FROM sources WHERE gateway_message_id = ?", (message_id,)).fetchone()
        if row:
            if row["job_id"] != job_id:
                raise ValueError("This gateway message already belongs to another job.")
            return row["id"]
    source_id = db.execute(
        """INSERT INTO sources(job_id,kind,sender,gateway_message_id,file_path,sha256,text,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (job_id, kind, sender, message_id, file_path, digest, text, timestamp()),
    ).lastrowid
    event(db, "captured", "source", source_id, actor=sender, job_id=job_id, source_kind=kind)
    return source_id


def _capture(
    db: Connection, job: dict, sender: str, text: str, message_id: str | None, media: tuple[Media, ...],
) -> dict:
    if message_id:
        db.execute("INSERT INTO inbound_messages(message_id,job_id) VALUES (?,?)", (message_id, job["id"]))
    source_ids = []
    if text or not media:
        source_ids.append(_attach(db, job["id"], "text", sender, text, message_id=message_id))
    for index, item in enumerate(media):
        evidence_id = f"{message_id}:media:{index}" if message_id else None
        media_text = item.get("text", "voice note, not transcribed" if item["kind"] == "voice" else "")
        source_ids.append(_attach(db, job["id"], item["kind"], sender, media_text,
                                  message_id=evidence_id, path=item["path"]))
    return _capture_result(job, duplicate=False) | {"source_ids": source_ids}


def _capture_result(job: dict, *, duplicate: bool) -> dict:
    return {"job_id": job["id"], "job_number": job["number"],
            "customer_id": job["customer_id"], "duplicate": duplicate}


def _captured_job(db: Connection, message_id: str | None) -> dict | None:
    if message_id:
        row = db.execute("SELECT job_id FROM inbound_messages WHERE message_id = ?", (message_id,)).fetchone()
        if row:
            return row_for(db, "jobs", row["job_id"])
    return None


def capture_enquiry(
    phone: str, *, text: str = "", message_id: str | None = None,
    media: Iterable[Media] = (), name: str | None = None,
) -> dict:
    """Capture a DM and return job_id, job_number, customer_id and duplicate."""
    phone_number(phone)
    text = text_field(text, "enquiry", optional=True, limit=40000)
    name = name.strip()[:200] if isinstance(name, str) else ""
    message_id = f"whatsapp:{phone}:{message_id}" if message_id else None
    media = tuple(media)
    with connect(write=True) as db:
        if phone in get_settings(db)["owner_phones"]:
            raise ValueError("Owner messages use the owner intake.")
        duplicate = _captured_job(db, message_id)
        if duplicate:
            return _capture_result(duplicate, duplicate=True)
        customer = _customer(db, name or phone, phone, "", "", phone)
        job = _enquiry_job(db, customer, text, phone)
        return _capture(db, job, phone, text, message_id, media)


def _enquiry_job(db: Connection, customer: dict, text: str, sender: str) -> dict:
    rows = db.execute(
        """SELECT * FROM jobs WHERE customer_id = ?
           AND state IN ('enquiry','quoted','accepted','scheduled','done','invoiced')
           ORDER BY id DESC""", (customer["id"],),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    since = timestamp(datetime.fromisoformat(timestamp()) - timedelta(hours=12))
    enquiries = [row for row in rows if row["state"] == "enquiry" and row["created_at"] >= since]
    if enquiries:
        return dict(enquiries[0])
    return _create_job(db, customer, text[:2000] or "Customer sent an attachment.", "", sender)


def capture_unresolved_enquiry(
    sender_id: str, *, text: str = "", message_id: str | None = None,
    media: Iterable[Media] = (), name: str | None = None,
) -> dict:
    """Preserve a phone-private WhatsApp enquiry under its stable LID identity."""
    if not isinstance(sender_id, str) or not re.fullmatch(r"[0-9]+(?::[0-9]+)?@lid", sender_id):
        raise ValueError("Expected a WhatsApp LID identity.")
    text = text_field(text, "enquiry", optional=True, limit=40000)
    name = name.strip()[:200] if isinstance(name, str) else ""
    message_id = f"whatsapp:{sender_id}:{message_id}" if message_id else None
    with connect(write=True) as db:
        duplicate = _captured_job(db, message_id)
        if duplicate:
            return _capture_result(duplicate, duplicate=True)
        customer = db.execute("SELECT * FROM customers WHERE gateway_sender=?", (sender_id,)).fetchone()
        if customer:
            customer = dict(customer)
        else:
            customer = _customer(db, name or "Customer with private phone", None, "",
                                 "WhatsApp phone unresolved. Confirm contact details before sending.", sender_id)
            db.execute("UPDATE customers SET gateway_sender=? WHERE id=?", (sender_id, customer["id"]))
        job = _enquiry_job(db, customer, text, sender_id)
        return _capture(db, job, sender_id, text, message_id, tuple(media))

def capture_owner(
    *, actor: str, text: str = "", job: int | str | None = None, customer_id: int | None = None,
    name: str | None = None, phone: str | None = None, address: str = "",
    message_id: str | None = None, media: Iterable[Media] = (),
) -> dict:
    """Create a new job for a specified customer, or attach evidence to an exact job."""
    text = text_field(text, "job description", optional=True, limit=40000)
    address = text_field(address, "site address", optional=True)
    media = tuple(media)
    if not text and not media:
        raise ValueError("Provide a description or media.")
    with connect(write=True) as db:
        require_owner(db, actor)
        duplicate = _captured_job(db, message_id)
        if duplicate:
            return _capture_result(duplicate, duplicate=True)
        if job is not None:
            selected = row_for(db, "jobs", job)
        else:
            customer = row_for(db, "customers", customer_id) if customer_id is not None else _customer(
                db, name or phone, phone, address, "", actor,
            )
            selected = _create_job(db, customer, text[:2000] or "Owner sent an attachment.", address, actor)
        return _capture(db, selected, actor, text, message_id, media)


def _job_state(db: Connection, job_id: int, state: str, actor: str, now: datetime | None = None) -> None:
    transition(db, "jobs", job_id, state, updated_at=timestamp(now))
    event(db, state, "job", job_id, actor=actor, now=now)


def _document_snapshot(db: Connection, job: dict, settings: dict) -> str:
    customer = row_for(db, "customers", job["customer_id"])
    return dumps({
        "business_name": settings["business_name"], "customer_name": customer["name"],
        "site_address": job["site_address"], "job_number": job["number"],
        "payment_text": settings["payment_text"],
    })


def draft_quote(
    job: int | str, items: list[dict], *, actor: str, valid_until: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Save a fresh immutable revision with totals calculated in code."""
    stamp = timestamp(now)
    with connect(write=True) as db:
        require_owner(db, actor)
        settings = get_settings(db)
        selected = row_for(db, "jobs", job)
        if selected["state"] not in ("enquiry", "quoted"):
            raise ValueError("Only enquiries and quoted jobs can receive a quote draft.")
        today = datetime.fromisoformat(stamp).astimezone(ZoneInfo(settings["timezone"])).date()
        valid_until = date_string(valid_until) if valid_until else (today + timedelta(days=30)).isoformat()
        if valid_until < today.isoformat():
            raise ValueError("Quote validity must include today.")
        lines = price_lines(db, selected["id"], items, settings)
        tax_rate = Decimal(settings["tax_rate"])
        amounts = money.totals(lines, tax_rate)
        previous = db.execute(
            "SELECT * FROM quotes WHERE job_id = ? ORDER BY revision DESC LIMIT 1", (selected["id"],),
        ).fetchone()
        number = previous["number"] if previous else allocate(db, "Q")
        revision = previous["revision"] + 1 if previous else 1
        if previous and previous["status"] in ("draft", "awaiting_approval", "sent"):
            transition(db, "quotes", previous["id"], "superseded")
            _cancel_outbox(db, related_id=previous["id"], kinds=("quote", "quote_followup"))
            _invalidate_pending(db, "issue_quote", previous["id"])
            _invalidate_pending(db, "quote_decision", previous["id"])
            event(db, "superseded", "quote", previous["id"], actor=actor, now=now)
        quote_id = db.execute(
            """INSERT INTO quotes(number,job_id,revision,lines,currency,subtotal_minor,tax_rate,tax_minor,
               total_minor,status,valid_until,created_at,document)
               VALUES (?,?,?,?,?,?,?,?,?,'draft',?,?,?)""",
            (number, selected["id"], revision, dumps(lines), settings["currency"], amounts["subtotal_minor"],
             str(tax_rate), amounts["tax_minor"], amounts["total_minor"], valid_until, stamp,
             _document_snapshot(db, selected, settings)),
        ).lastrowid
        event(db, "drafted", "quote", quote_id, actor=actor, now=now, number=number, revision=revision)
        return row_for(db, "quotes", quote_id) | {"lines": lines}


def _cancel_outbox(
    db: Connection, *, kinds: tuple[str, ...], related_id: int | None = None, job_id: int | None = None,
) -> None:
    field, value = ("related_id", related_id) if related_id is not None else ("job_id", job_id)
    placeholders = ",".join("?" for _ in kinds)
    rows = db.execute(
        f"""UPDATE outbox SET status = 'cancelled', finished_at = ?, detail = 'Related record changed.' WHERE {field} = ? AND kind IN ({placeholders})
            AND status IN ('draft','awaiting_approval','approved') RETURNING id, approval_id""",
        (timestamp(), value, *kinds),
    ).fetchall()
    for row in rows:
        if row["approval_id"]:
            db.execute("UPDATE approvals SET status = 'invalidated' WHERE id = ? AND status = 'pending'",
                       (row["approval_id"],))
        event(db, "cancelled", "outbox", row["id"])
        _notify_delivery(db, row_for(db, "outbox", row["id"]), "cancelled", "Related record changed.", None)


def _decision_quote(db: Connection, quote: int | str, revision: int | None) -> dict:
    selected = row_for(db, "quotes", quote)
    if ((isinstance(quote, str) and selected["revision"] > 1 and revision is None)
            or (revision is not None and selected["revision"] != revision)):
        raise ValueError("Specify the exact quote revision.")
    return selected


def _check_quote_decision(db: Connection, quote: dict, decision: str, now: datetime | None) -> None:
    if decision not in ("accepted", "declined"):
        raise ValueError("Quote decision must be accepted or declined.")
    today = datetime.fromisoformat(timestamp(now)).astimezone(ZoneInfo(get_settings(db)["timezone"])).date()
    if quote["valid_until"] < today.isoformat():
        raise ValueError("The quote has expired. Create a new revision.")
    if quote["status"] != "sent":
        raise ValueError("A decision requires an issued quote.")


def _record_quote_decision(db: Connection, quote: dict, decision: str, actor: str, now: datetime | None) -> dict:
    _check_quote_decision(db, quote, decision, now)
    transition(db, "quotes", quote["id"], decision)
    _job_state(db, quote["job_id"], decision, actor, now)
    _cancel_outbox(db, related_id=quote["id"], kinds=("quote_followup", "quote"))
    event(db, decision, "quote", quote["id"], actor=actor, now=now)
    return quote | {"status": decision}


def record_quote_decision(
    quote: int | str, decision: str, *, actor: str, revision: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Record an explicit owner-entered decision on the exact issued quote."""
    with connect(write=True) as db:
        require_owner(db, actor)
        selected = _decision_quote(db, quote, revision)
        result = _record_quote_decision(db, selected, decision, actor, now)
        _invalidate_pending(db, "quote_decision", selected["id"])
        return result


def request_quote_decision(
    quote: int | str, decision: str, *, actor: str, revision: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Propose a decision on one issued quote revision for the owner to confirm."""
    with connect(write=True) as db:
        require_owner(db, actor)
        selected = _decision_quote(db, quote, revision)
        _check_quote_decision(db, selected, decision, now)
        _invalidate_if_changed(db, "quote_decision", selected["id"], {"decision": decision})
        approval = _new_approval(
            db, "quote_decision", f"Mark {selected['number']} revision {selected['revision']} {decision}",
            now=now, record_id=selected["id"], decision=decision, document_hash=_document_hash(selected),
        )
        return _ticket(approval)

def propose_extra(job: int | str, description: str, amount: str | int | float, *, actor: str) -> dict:
    """Store a proposed extra without adding it to the invoice."""
    with connect(write=True) as db:
        require_owner(db, actor)
        selected = row_for(db, "jobs", job)
        if selected["state"] not in ("accepted", "scheduled", "done"):
            raise ValueError("Extras belong to accepted, scheduled or completed jobs.")
        if db.execute("SELECT 1 FROM invoices WHERE job_id=? AND status <> 'void'", (selected["id"],)).fetchone():
            raise ValueError("Void the existing invoice before adding an extra.")
        amount_minor = money.minor(amount, get_settings(db)["currency"])
        if amount_minor <= 0:
            raise ValueError("Extra amount must be positive.")
        extra_id = db.execute(
            """INSERT INTO variations(job_id,description,amount_minor,status,created_at)
               VALUES (?,?,?,'proposed',?)""",
            (selected["id"], text_field(description, "extra description"), amount_minor, timestamp()),
        ).lastrowid
        event(db, "proposed", "variation", extra_id, actor=actor)
        return row_for(db, "variations", extra_id)


def _record_extra_decision(
    db: Connection, extra_id: int, source_id: int, decision: str, actor: str,
    now: datetime | None = None,
) -> dict:
    extra = row_for(db, "variations", extra_id)
    source = row_for(db, "sources", source_id)
    if decision not in ("agreed", "declined") or extra["job_id"] != source["job_id"] or not (
        source["text"] or source["file_path"]
    ):
        raise ValueError("Provide agreement evidence from the same job.")
    if db.execute("SELECT 1 FROM invoices WHERE job_id=? AND status <> 'void'", (extra["job_id"],)).fetchone():
        raise ValueError("Void the existing invoice before recording an extra decision.")
    transition(db, "variations", extra_id, decision, source_id=source_id)
    event(db, decision, "variation", extra_id, actor=actor, now=now, source_id=source_id)
    return extra | {"status": decision, "source_id": source_id}


def schedule(
    job: int | str, start: str | datetime, end: str | datetime, *, actor: str,
    now: datetime | None = None,
) -> dict:
    """Schedule an accepted job or replace its prior appointment."""
    with connect(write=True) as db:
        require_owner(db, actor)
        return _schedule(db, job, start, end, actor, now)


def _schedule(
    db: Connection, job: int | str, start: str | datetime, end: str | datetime,
    actor: str, now: datetime | None,
) -> dict:
    """Commit a validated appointment inside the caller's transaction."""
    settings = get_settings(db)
    selected = row_for(db, "jobs", job)
    if selected["state"] not in ("accepted", "scheduled"):
        raise ValueError("Accept the quote before scheduling this job.")
    start_utc = parse_instant(start, settings["timezone"])
    end_utc = parse_instant(end, settings["timezone"])
    stamp = timestamp(now)
    if not stamp < start_utc < end_utc:
        raise ValueError("Schedule a future start and an end after the start.")
    active = db.execute(
        "SELECT * FROM appointments WHERE job_id = ? AND status = 'scheduled'", (selected["id"],),
    ).fetchone()
    if active and (active["start"], active["end"]) == (start_utc, end_utc):
        return dict(active)
    if active:
        _cancel_appointment(db, active["id"], "cancelled", actor, now)
    appointment_id = db.execute(
        """INSERT INTO appointments(job_id,start,end,created_at,status) VALUES (?,?,?,?,'scheduled')""",
        (selected["id"], start_utc, end_utc, stamp),
    ).lastrowid
    if selected["state"] == "accepted":
        _job_state(db, selected["id"], "scheduled", actor, now)
    event(db, "scheduled", "appointment", appointment_id, actor=actor, now=now)
    return row_for(db, "appointments", appointment_id)


def _cancel_appointment(
    db: Connection, appointment_id: int, status: str, actor: str, now: datetime | None = None,
) -> None:
    transition(db, "appointments", appointment_id, status)
    _cancel_outbox(db, related_id=appointment_id, kinds=("appointment_reminder",))
    event(db, status, "appointment", appointment_id, actor=actor, now=now)


def cancel_job(job: int | str, *, actor: str) -> dict:
    """Cancel open work, its appointment and any pending customer sends."""
    with connect(write=True) as db:
        require_owner(db, actor)
        selected = row_for(db, "jobs", job)
        _job_state(db, selected["id"], "cancelled", actor)
        for row in db.execute("SELECT id FROM appointments WHERE job_id=? AND status='scheduled'", (selected["id"],)):
            _cancel_appointment(db, row["id"], "cancelled", actor)
        for row in db.execute(
            "SELECT id FROM quotes WHERE job_id=? AND status IN ('draft','awaiting_approval','sent')", (selected["id"],),
        ):
            transition(db, "quotes", row["id"], "superseded")
            _invalidate_pending(db, "issue_quote", row["id"])
            _invalidate_pending(db, "quote_decision", row["id"])
            event(db, "superseded", "quote", row["id"], actor=actor)
        _cancel_outbox(db, job_id=selected["id"], kinds=("quote", "quote_followup", "enquiry_ack"))
        return selected | {"state": "cancelled"}


def complete_job(
    job: int | str, *, actor: str, items: list[dict] | None = None, now: datetime | None = None,
) -> dict:
    """Record completion and optional line items for work without a quote."""
    with connect(write=True) as db:
        require_owner(db, actor)
        return _complete_job(db, job, actor, items, now)


def _complete_job(db: Connection, job: int | str, actor: str, items: list[dict] | None, now: datetime | None) -> dict:
    """Commit completion inside the caller's transaction."""
    selected = row_for(db, "jobs", job)
    if selected["state"] not in ("enquiry", "accepted", "scheduled"):
        raise ValueError("Only open, uninvoiced work can be completed.")
    accepted = db.execute("SELECT id FROM quotes WHERE job_id=? AND status='accepted'", (selected["id"],)).fetchone()
    if accepted and items is not None:
        raise ValueError("The accepted quote supplies the invoice lines. Record extras separately.")
    lines = price_lines(db, selected["id"], items, get_settings(db)) if items is not None else None
    _job_state(db, selected["id"], "done", actor, now)
    if lines is not None:
        db.execute("UPDATE jobs SET completion_lines = ? WHERE id = ?", (dumps(lines), selected["id"]))
    for row in db.execute("SELECT id FROM appointments WHERE job_id=? AND status='scheduled'", (selected["id"],)):
        _cancel_appointment(db, row["id"], "completed", actor)
    # An enquiry can be completed without taking a draft quote forward.
    for row in db.execute(
        "SELECT id FROM quotes WHERE job_id=? AND status IN ('draft','awaiting_approval')", (selected["id"],),
    ):
        transition(db, "quotes", row["id"], "superseded")
        _invalidate_pending(db, "issue_quote", row["id"])
        _invalidate_pending(db, "quote_decision", row["id"])
        event(db, "superseded", "quote", row["id"], actor=actor)
    _cancel_outbox(db, job_id=selected["id"], kinds=("quote", "quote_followup"))
    return row_for(db, "jobs", selected["id"])


def draft_invoice(
    job: int | str, *, actor: str, items: list[dict] | None = None, now: datetime | None = None,
) -> dict:
    """Snapshot the accepted quote or completion lines, plus only agreed extras."""
    stamp = timestamp(now)
    with connect(write=True) as db:
        require_owner(db, actor)
        settings = get_settings(db)
        selected = row_for(db, "jobs", job)
        if selected["state"] != "done":
            raise ValueError("Complete the job before drafting its invoice.")
        existing = db.execute("SELECT * FROM invoices WHERE job_id=? AND status <> 'void'", (selected["id"],)).fetchone()
        if existing:
            if items is not None:
                raise ValueError("Void the existing invoice before changing its lines.")
            return dict(existing) | {"lines": json.loads(existing["lines"])}
        quote = db.execute("SELECT * FROM quotes WHERE job_id=? AND status='accepted'", (selected["id"],)).fetchone()
        if quote:
            if items is not None:
                raise ValueError("The accepted quote supplies the invoice lines.")
            lines = json.loads(quote["lines"])
            tax_rate = Decimal(quote["tax_rate"])
        else:
            if items is not None and selected["completion_lines"]:
                raise ValueError("The recorded completion supplies the invoice lines.")
            lines = (price_lines(db, selected["id"], items, settings) if items is not None
                     else json.loads(selected["completion_lines"] or "[]"))
            tax_rate = Decimal(settings["tax_rate"])
        if not lines:
            raise ValueError("Provide completion lines for work without an accepted quote.")
        for extra in db.execute("SELECT * FROM variations WHERE job_id=? AND status='agreed' ORDER BY id", (selected["id"],)):
            lines.append({
                "description": extra["description"], "kind": "other", "qty": "1",
                "unit_price_minor": extra["amount_minor"], "total_minor": extra["amount_minor"],
                "variation_id": extra["id"],
            })
        amounts = money.totals(lines, tax_rate)
        if amounts["total_minor"] <= 0:
            raise ValueError("Invoice total must be positive.")
        number = allocate(db, "INV")
        local = datetime.fromisoformat(stamp).astimezone(ZoneInfo(settings["timezone"]))
        due_at = (local.date() + timedelta(days=settings["payment_terms_days"])).isoformat()
        invoice_id = db.execute(
            """INSERT INTO invoices(number,job_id,quote_id,lines,currency,subtotal_minor,tax_rate,tax_minor,
               total_minor,created_at,due_at,status,document) VALUES (?,?,?,?,?,?,?,?,?,?,?,'draft',?)""",
            (number, selected["id"], quote["id"] if quote else None, dumps(lines), settings["currency"],
             amounts["subtotal_minor"], str(tax_rate), amounts["tax_minor"], amounts["total_minor"], stamp,
             due_at, _document_snapshot(db, selected, settings)),
        ).lastrowid
        event(db, "drafted", "invoice", invoice_id, actor=actor, now=now, number=number)
        return row_for(db, "invoices", invoice_id) | {"lines": lines}


def set_invoice_status(invoice: int | str, status: str, *, actor: str) -> dict:
    """Hold, resume or void an invoice after checking its lifecycle and balance."""
    if status not in ("on_hold", "sent", "void"):
        raise ValueError("Choose hold, resume or void.")
    with connect(write=True) as db:
        require_owner(db, actor)
        selected = row_for(db, "invoices", invoice)
        if status == "sent" and selected["status"] != "on_hold":
            raise ValueError("Only a held invoice can resume.")
        if status == "void" and db.execute("SELECT 1 FROM payments WHERE invoice_id=?", (selected["id"],)).fetchone():
            raise ValueError("An invoice with a recorded payment cannot be voided.")
        transition(db, "invoices", selected["id"], status)
        if status in ("void", "on_hold"):
            _cancel_outbox(db, related_id=selected["id"], kinds=("invoice", "payment_chaser"))
        if status == "void" and selected["issued_at"]:
            changed = db.execute(
                "UPDATE jobs SET state='done',updated_at=? WHERE id=? AND state='invoiced'",
                (timestamp(), selected["job_id"]),
            ).rowcount
            if changed != 1:
                raise ValueError("Only an invoiced job can reopen after voiding.")
            event(db, "reopened_after_void", "job", selected["job_id"], actor=actor)
        if status == "void":
            _invalidate_pending(db, "issue_invoice", selected["id"])
        event(db, status, "invoice", selected["id"], actor=actor)
        return selected | {"status": status}


def _record_payment(
    db: Connection, invoice: dict, amount_minor: int, payment_date: str, method: str, reference: str,
    actor: str, now: datetime | None = None, request_key: str | None = None,
) -> dict:
    require_owner(db, actor)
    paid = db.execute(
        "SELECT COALESCE(SUM(amount_minor),0) FROM payments WHERE invoice_id = ?", (invoice["id"],),
    ).fetchone()[0]
    if invoice["status"] not in ("sent", "on_hold") or not 0 < amount_minor <= invoice["total_minor"] - paid:
        raise ValueError("Payment must be positive and within the outstanding balance of an issued invoice.")
    payment_id = db.execute(
        """INSERT INTO payments(invoice_id,amount_minor,date,method,reference,recorded_by,created_at,request_key)
           VALUES (?,?,?,?,?,?,?,?)""",
        (invoice["id"], amount_minor, payment_date, method, reference, actor, timestamp(now), request_key),
    ).lastrowid
    balance = invoice["total_minor"] - paid - amount_minor
    _cancel_outbox(db, related_id=invoice["id"], kinds=("payment_chaser",))
    if balance == 0:
        transition(db, "invoices", invoice["id"], "paid")
        _job_state(db, invoice["job_id"], "paid", actor, now)
        _cancel_outbox(db, related_id=invoice["id"], kinds=("invoice",))
    event(db, "recorded", "payment", payment_id, actor=actor, now=now, invoice_id=invoice["id"], balance_minor=balance)
    return row_for(db, "payments", payment_id) | {"balance_minor": balance}


def propose_receipt(
    path: str | Path, fields: dict, *, actor: str, job: int | str | None = None,
    category: str = "materials",
) -> dict:
    """Save a receipt as a proposed expense and report same-amount candidates."""
    with connect(write=True) as db:
        require_owner(db, actor)
        copied = copy_source(path)
        existing = db.execute("SELECT * FROM expenses WHERE receipt_sha256 = ?", (copied[1],)).fetchone()
        if existing:
            expense = dict(existing)
            if job is not None:
                selected = row_for(db, "jobs", job)
                if expense["job_id"] is None:
                    expense = _assign_expense(db, expense, selected, actor)
                elif expense["job_id"] != selected["id"]:
                    return expense | {"duplicate": True, "job_conflict": True}
            return expense | {"duplicate": True}
        settings = get_settings(db)
        selected = row_for(db, "jobs", job) if job is not None else None
        vendor = text_field(fields.get("vendor"), "vendor", limit=200)
        vendor_key = " ".join(vendor.casefold().split())
        payment_date = date_string(fields.get("date"))
        currency = fields.get("currency")
        if currency != settings["currency"]:
            raise ValueError("Receipt currency must match the business currency.")
        amount_minor = money.minor(fields.get("total"), currency)
        if amount_minor <= 0:
            raise ValueError("Receipt total must be positive.")
        confidence = fields.get("confidence")
        if confidence not in ("high", "low"):
            raise ValueError("Receipt extraction must supply a confidence level.")
        candidates = [dict(row) for row in db.execute(
            """SELECT id,job_id,status FROM expenses WHERE vendor_key=? AND date=? AND amount_minor=?
               ORDER BY id""",
            (vendor_key, payment_date, amount_minor),
        )]
        source_id = _attach(
            db, selected["id"], "receipt", actor, dumps(fields), copied=copied,
        ) if selected else None
        expense_id = db.execute(
            """INSERT INTO expenses(date,vendor,vendor_key,amount_minor,currency,category,job_id,source_id,
               receipt_path,receipt_sha256,status,confidence,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,'proposed',?,?)""",
            (payment_date, vendor, vendor_key, amount_minor, currency, text_field(category, "category", limit=100),
             selected["id"] if selected else None, source_id, *copied, confidence, timestamp()),
        ).lastrowid
        event(db, "proposed", "expense", expense_id, actor=actor, possible_duplicates=[row["id"] for row in candidates])
        return row_for(db, "expenses", expense_id) | {"duplicate": False, "possible_duplicates": candidates}


def _assign_expense(db: Connection, expense: dict, job: dict, actor: str) -> dict:
    if expense["job_id"] is not None and expense["job_id"] != job["id"]:
        raise ValueError("The receipt is already attached to another job.")
    source_id = _attach(db, job["id"], "receipt", actor, f"Receipt from {expense['vendor']}",
                        copied=(expense["receipt_path"], expense["receipt_sha256"]))
    db.execute("UPDATE expenses SET job_id=?,source_id=? WHERE id=?", (job["id"], source_id, expense["id"]))
    _invalidate_pending(db, "confirm_expense", expense["id"])
    event(db, "attached_to_job", "expense", expense["id"], actor=actor, job_id=job["id"])
    return expense | {"job_id": job["id"], "source_id": source_id}


def assign_expense(expense_id: int, job: int | str, *, actor: str) -> dict:
    """Attach an unassigned receipt to an exact job, preserving its original amount."""
    with connect(write=True) as db:
        require_owner(db, actor)
        return _assign_expense(db, row_for(db, "expenses", expense_id), row_for(db, "jobs", job), actor)

def _confirm_expense(
    db: Connection, expense_id: int, actor: str, now: datetime | None = None,
) -> dict:
    expense = row_for(db, "expenses", expense_id)
    if expense["confidence"] != "high":
        raise ValueError("Low-confidence extraction needs corrected fields before confirmation.")
    transition(db, "expenses", expense_id, "confirmed")
    event(db, "confirmed", "expense", expense_id, actor=actor, now=now)
    return expense | {"status": "confirmed"}




def _invalidate_pending(db: Connection, action: str, record_id: int) -> None:
    rows = db.execute(
        """UPDATE approvals SET status='invalidated'
           WHERE action=? AND status='pending' AND json_extract(payload,'$.record_id')=?
           RETURNING id""",
        (action, record_id),
    ).fetchall()
    for row in rows:
        event(db, "invalidated", "approval", row["id"])


def _new_approval(
    db: Connection, action: str, summary: str, *, now: datetime | None = None, **payload: object,
) -> dict:
    stamp = timestamp(now)
    payload = {"summary": summary, "owner_binding": owner_binding(get_settings(db)), **payload}
    digest = payload_hash(payload)
    existing = db.execute(
        """SELECT * FROM approvals WHERE action=? AND payload_sha256=? AND status='pending'
           AND expires_at > ? ORDER BY id DESC LIMIT 1""",
        (action, digest, stamp),
    ).fetchone()
    if existing:
        return dict(existing)
    code = "A" + allocate(db, "A").split("-", 1)[1].lstrip("0")
    expires = timestamp(datetime.fromisoformat(stamp) + timedelta(hours=24))
    approval_id = db.execute(
        """INSERT INTO approvals(code,action,payload,payload_sha256,created_at,expires_at,status)
           VALUES (?,?,?,?,?,?,'pending')""",
        (code, action, dumps(payload), digest, stamp, expires),
    ).lastrowid
    event(db, "requested", "approval", approval_id, now=now, action=action, code=code, payload_sha256=digest)
    return row_for(db, "approvals", approval_id)


def _ticket(approval: dict, **result: object) -> dict:
    return {
        "code": approval["code"], "expires_at": approval["expires_at"],
        "status": "awaiting_approval", "summary": json.loads(approval["payload"])["summary"], **result,
    }


def _store_outbox(
    db: Connection, kind: str, recipient: str, body: str, stage_key: str, *,
    job_id: int | None, related_id: int | None, standing_hash: str | None = None,
    attachment_path: str | Path | None = None, now: datetime | None = None,
    status: str = "awaiting_approval",
) -> dict:
    path, digest = attachment(attachment_path)
    payload = {
        "kind": kind, "recipient": phone_number(recipient), "body": message_text(body),
        "attachment_path": path, "attachment_sha256": digest, "stage_key": text_field(stage_key, "stage key", limit=500),
        "job_id": job_id, "related_id": related_id, "standing_hash": standing_hash,
    }
    outbox_id = db.execute(
        """INSERT INTO outbox(kind,recipient,body,attachment_path,attachment_sha256,stage_key,job_id,
           related_id,standing_hash,payload_sha256,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (*payload.values(), payload_hash(payload), status, timestamp(now)),
    ).lastrowid
    event(db, "queued", "outbox", outbox_id, now=now, message_kind=kind)
    return row_for(db, "outbox", outbox_id)


def _get_stage(db: Connection, stage_key: str) -> dict | None:
    row = db.execute("SELECT * FROM outbox WHERE stage_key=?", (stage_key,)).fetchone()
    return dict(row) if row else None


def _recipient(db: Connection, job_id: int) -> str | None:
    return db.execute(
        "SELECT c.phone FROM jobs j JOIN customers c ON c.id=j.customer_id WHERE j.id=?", (job_id,),
    ).fetchone()[0]


def _render_template(db: Connection, kind: str, job_id: int, related_id: int | None) -> tuple[str, str] | None:
    settings = get_settings(db)
    template = settings["templates"].get(kind)
    if not template or template["approved_hash"] != payload_hash(template["text"]):
        return None
    job = row_for(db, "jobs", job_id)
    customer = row_for(db, "customers", job["customer_id"])
    fields = {
        "business_name": settings["business_name"], "customer_name": customer["name"],
        "job_number": job["number"], "review_link": settings["review_link"],
    }
    if kind == "appointment_reminder":
        appointment = row_for(db, "appointments", related_id)
        start = datetime.fromisoformat(appointment["start"]).astimezone(ZoneInfo(settings["timezone"]))
        fields["appointment_start"] = start.strftime("%Y-%m-%d %H:%M %Z")
    if kind == "review_request" and "{review_link}" in template["text"] and not fields["review_link"]:
        return None
    fields = {key: display_text(value) for key, value in fields.items()}
    return message_text(template["text"].format_map(fields)), template["approved_hash"]


def _outbox_live(db: Connection, row: dict, now: datetime | None, *, for_claim: bool = False) -> bool:
    kind = row["kind"]
    settings = get_settings(db)
    if kind == "owner_notice":
        return row["recipient"] in settings["owner_phones"]
    if row["job_id"] is not None:
        job = row_for(db, "jobs", row["job_id"])
        if job["state"] in ("cancelled", "declined") or _recipient(db, job["id"]) != row["recipient"]:
            return False
    if row["standing_hash"] is not None:
        rendered = _render_template(db, kind, row["job_id"], row["related_id"])
        if not rendered or rendered != (row["body"], row["standing_hash"]):
            return False
    if kind in ("quote_followup", "payment_chaser"):
        original = "quote" if kind == "quote_followup" else "invoice"
        if not db.execute("SELECT 1 FROM outbox WHERE kind=? AND related_id=? AND status='sent'",
                          (original, row["related_id"])).fetchone():
            return False
    if kind in ("quote", "quote_followup"):
        quote = row_for(db, "quotes", row["related_id"])
        expected = "sent" if for_claim or kind == "quote_followup" else "awaiting_approval"
        today = datetime.fromisoformat(timestamp(now)).astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
        valid = quote["job_id"] == row["job_id"] and quote["status"] == expected and quote["valid_until"] >= today
        if kind == "quote_followup":
            from .followups import quote_followup

            action = quote_followup(quote | {"phone": row["recipient"]}, datetime.fromisoformat(timestamp(now)), date.fromisoformat(today))
            return valid and bool(action) and action["stage_key"] == row["stage_key"] and action["body"] == row["body"]
        return valid
    if kind in ("invoice", "payment_chaser"):
        invoice = row_for(db, "invoices", row["related_id"])
        expected = "sent" if for_claim or kind == "payment_chaser" else "awaiting_approval"
        valid = invoice["job_id"] == row["job_id"] and invoice["status"] == expected
        if kind == "payment_chaser":
            from .followups import payment_chaser

            paid = db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM payments WHERE invoice_id=?", (invoice["id"],)).fetchone()[0]
            candidate = invoice | {"phone": row["recipient"], "balance_minor": invoice["total_minor"] - paid}
            today = datetime.fromisoformat(timestamp(now)).astimezone(ZoneInfo(settings["timezone"])).date()
            action = payment_chaser(candidate, today)
            return valid and bool(action) and action["stage_key"] == row["stage_key"] and action["body"] == row["body"]
        return valid
    if kind == "appointment_reminder":
        appointment = row_for(db, "appointments", row["related_id"])
        from .followups import appointment_reminder

        action = appointment_reminder(appointment | {"phone": row["recipient"]}, datetime.fromisoformat(timestamp(now)))
        return (appointment["job_id"] == row["job_id"] and job["state"] == "scheduled"
                and bool(action) and action["stage_key"] == row["stage_key"])
    if kind == "review_request":
        return job["state"] == "paid" and job["updated_at"] > settings.get("setup_at", timestamp(now))
    return True


def enqueue_message(
    kind: str, recipient: str, body: str, stage_key: str, *,
    attachment_path: str | Path | None = None, standing_template: str | None = None,
    job_id: int | None = None, related_id: int | None = None, now: datetime | None = None,
) -> dict | None:
    """Queue one customer action per stage. Acknowledgements reserve a rolling 12h slot."""
    if kind not in (*TEMPLATES, "quote_followup", "payment_chaser", "other"):
        raise ValueError("Unsupported customer message kind.")
    phone_number(recipient)
    if kind in TEMPLATES and (standing_template != kind or job_id is None or attachment_path is not None):
        raise ValueError("Use the standing template and job for this message kind.")
    if kind not in TEMPLATES and standing_template is not None:
        raise ValueError("This message needs its own approval.")
    if kind in ("quote_followup", "payment_chaser", "appointment_reminder") and (
        job_id is None or related_id is None
    ):
        raise ValueError("This scheduled message needs its job and source record.")
    with connect(write=True) as db:
        if _get_stage(db, stage_key):
            return None
        standing_hash = None
        if standing_template:
            rendered = _render_template(db, kind, job_id, related_id)
            if rendered is None:
                return None
            body, standing_hash = rendered
        candidate = {"kind": kind, "job_id": job_id, "related_id": related_id,
                     "recipient": recipient, "body": body, "standing_hash": standing_hash,
                     "stage_key": stage_key}
        if not _outbox_live(db, candidate, now):
            return None
        if kind == "enquiry_ack":
            since = timestamp(datetime.fromisoformat(timestamp(now)) - timedelta(hours=12))
            if db.execute(
                """SELECT 1 FROM outbox WHERE kind='enquiry_ack' AND recipient=?
                   AND (status IN ('approved','sending') OR
                        (status IN ('sent','unknown') AND COALESCE(finished_at,created_at) > ?))""",
                (recipient, since),
            ).fetchone():
                return None
        row = _store_outbox(
            db, kind, recipient, body, stage_key, job_id=job_id, related_id=related_id,
            standing_hash=standing_hash, attachment_path=attachment_path, now=now,
            status="approved" if standing_hash else "awaiting_approval",
        )
        if not standing_hash:
            approval = _new_approval(
                db, "send", f"{kind.replace('_', ' ').capitalize()} to {recipient}",
                now=now, outbox_id=row["id"], message=message_payload(row),
            )
            db.execute("UPDATE outbox SET approval_id=? WHERE id=?", (approval["id"], row["id"]))
            row |= {"approval_id": approval["id"], "code": approval["code"]}
        return row


def enqueue_owner_notice(
    recipient: str, body: str, stage_key: str, *, now: datetime | None = None,
) -> dict:
    """Queue one internal notice addressed to a configured WhatsApp owner."""
    with connect(write=True) as db:
        require_owner(db, phone_number(recipient), cli=False)
        existing = _get_stage(db, stage_key)
        if existing:
            if existing["kind"] != "owner_notice" or existing["recipient"] != recipient:
                raise ValueError("This stage key is already in use.")
            return existing
        return _store_outbox(db, "owner_notice", recipient, body, stage_key,
                             job_id=None, related_id=None, status="approved", now=now)


def propose_template(kind: str, text: str, *, actor: str, now: datetime | None = None) -> dict:
    """Request standing approval for one exact template text."""
    text = template_text(kind, text)
    with connect(write=True) as db:
        require_owner(db, actor)
        settings = get_settings(db)
        previous = settings["templates"].get(kind)
        if previous and previous["text"] == text and previous["approved_hash"] == payload_hash(text):
            return {"status": "approved", "kind": kind}
        if not previous or previous["text"] != text:
            db.execute(
                """UPDATE approvals SET status='invalidated' WHERE action='template' AND status='pending'
                   AND json_extract(payload,'$.kind')=?""",
                (kind,),
            )
            settings["templates"][kind] = {"text": text, "approved_hash": None}
            db.execute("INSERT OR REPLACE INTO settings(id,data) VALUES (1,?)", (dumps(settings),))
        approval = _new_approval(db, "template", f"Standing template: {kind}", now=now, kind=kind, text=text)
        return _ticket(approval, kind=kind, text=text)



def request_issue(
    kind: str, reference: int | str, *, actor: str,
    now: datetime | None = None,
) -> dict:
    """Request issuance of a quote revision or invoice, binding the exact customer payload."""
    if kind not in ("quote", "invoice"):
        raise ValueError("Choose a quote or invoice.")
    table = f"{kind}s"
    action = f"issue_{kind}"
    stamp = timestamp(now)
    with connect(write=True) as db:
        require_owner(db, actor)
        record = row_for(db, table, reference)
        job = row_for(db, "jobs", record["job_id"])
        allowed = ("enquiry", "quoted") if kind == "quote" else ("done",)
        if job["state"] not in allowed or record["status"] not in ("draft", "awaiting_approval"):
            raise ValueError(f"This {kind} cannot be issued in its current state.")
        if kind == "quote":
            today = datetime.fromisoformat(stamp).astimezone(ZoneInfo(get_settings(db)["timezone"])).date().isoformat()
            if record["valid_until"] < today:
                raise ValueError("The quote has expired. Create a new revision.")
        prior = db.execute(
            """SELECT * FROM approvals WHERE action=? AND status='pending' AND expires_at>?
               AND json_extract(payload,'$.record_id')=? ORDER BY id DESC LIMIT 1""",
            (action, stamp, record["id"]),
        ).fetchone()
        from .documents import document_pdf

        attachment_path = document_pdf(kind, record)
        path, digest = attachment(attachment_path)
        recipient = _recipient(db, job["id"])
        if prior:
            payload = json.loads(prior["payload"])
            prior_outbox = row_for(db, "outbox", payload["outbox_id"]) if payload["outbox_id"] else None
            if (payload["owner_binding"] == owner_binding(get_settings(db)) and payload["recipient"] == recipient
                    and payload["attachment_path"] == path and payload["attachment_sha256"] == digest
                    and (prior_outbox is None or _message_matches(prior_outbox, payload))):
                return _ticket(dict(prior), outbox_id=payload["outbox_id"], number=record["number"], attachment_path=path)
        if record["status"] == "awaiting_approval":
            _invalidate_pending(db, action, record["id"])
            _cancel_outbox(db, related_id=record["id"], kinds=(kind,))
        else:
            transition(db, table, record["id"], "awaiting_approval")
        revision = f" revision {record['revision']}" if kind == "quote" else ""
        summary = f"{kind.capitalize()} {record['number']}{revision}, {record['currency']} {money.major(record['total_minor'], record['currency'])}"
        body = f"{kind.capitalize()} {record['number']}{revision} for your job {job['number']}."
        outbox = None
        if recipient:
            stage_key = f"{kind}:{record['id']}"
            old = _get_stage(db, stage_key)
            if old:
                if old["status"] != "cancelled" or old["claimed_at"]:
                    raise ValueError("Delivery has already started for this document.")
                outbox = old | {"body": body, "recipient": recipient, "attachment_path": path, "attachment_sha256": digest}
                db.execute(
                    """UPDATE outbox SET body=?,recipient=?,attachment_path=?,attachment_sha256=?,
                       payload_sha256=?,status='awaiting_approval' WHERE id=?""",
                    (body, recipient, path, digest, payload_hash(message_payload(outbox)), old["id"]),
                )
            else:
                outbox = _store_outbox(db, kind, recipient, body, stage_key, attachment_path=path,
                                       job_id=job["id"], related_id=record["id"], now=now)
        approval = _new_approval(
            db, action, summary, now=now, record_id=record["id"], document_hash=_document_hash(record),
            recipient=recipient, outbox_id=outbox["id"] if outbox else None,
            message=message_payload(outbox) if outbox else None, attachment_path=path, attachment_sha256=digest,
        )
        if outbox:
            db.execute("UPDATE outbox SET approval_id=? WHERE id=?", (approval["id"], outbox["id"]))
        return _ticket(approval, outbox_id=outbox["id"] if outbox else None, number=record["number"], attachment_path=path)


def _document_hash(row: dict) -> str:
    return payload_hash({key: value for key, value in row.items() if key not in ("status", "issued_at", "delivery_status")})


def request_extra_decision(
    extra_id: int, source_id: int, decision: str = "agreed", *, actor: str, now: datetime | None = None,
) -> dict:
    """Ask the owner to confirm the exact extra and its same-job agreement evidence."""
    with connect(write=True) as db:
        require_owner(db, actor)
        extra = row_for(db, "variations", extra_id)
        source = row_for(db, "sources", source_id)
        if (decision not in ("agreed", "declined") or extra["status"] != "proposed"
                or extra["job_id"] != source["job_id"] or not (source["text"] or source["file_path"])):
            raise ValueError("Use a proposed extra and agreement evidence from the same job.")
        _invalidate_if_changed(db, "extra_decision", extra_id, {"source_id": source_id, "decision": decision})
        currency = get_settings(db)["currency"]
        approval = _new_approval(
            db, "extra_decision", f"Record {decision}: {extra['description']} ({currency} {money.major(extra['amount_minor'], currency)})",
            now=now, record_id=extra_id, source_id=source_id, decision=decision,
            extra_hash=payload_hash(extra), evidence_hash=payload_hash(source),
        )
        return _ticket(approval)


def _invalidate_if_changed(db: Connection, action: str, record_id: int, fields: dict) -> None:
    for row in db.execute(
        """SELECT id,payload FROM approvals WHERE action=? AND status='pending'
           AND json_extract(payload,'$.record_id')=?""",
        (action, record_id),
    ):
        payload = json.loads(row["payload"])
        if any(payload.get(key) != value for key, value in fields.items()):
            db.execute("UPDATE approvals SET status='invalidated' WHERE id=?", (row["id"],))
            event(db, "invalidated", "approval", row["id"])


def request_expense_confirmation(
    expense_id: int, *, actor: str, now: datetime | None = None,
) -> dict:
    """Request confirmation of a high-confidence expense, exposing possible duplicates."""
    with connect(write=True) as db:
        require_owner(db, actor)
        expense = row_for(db, "expenses", expense_id)
        if expense["status"] != "proposed" or expense["confidence"] != "high":
            raise ValueError("Expense must be proposed with high confidence or corrected by the owner.")
        similar = _similar_expenses(db, expense)
        summary = f"Confirm {expense['vendor']} receipt, {expense['currency']} {money.major(expense['amount_minor'], expense['currency'])}"
        if similar:
            summary += f". Possible duplicates: {', '.join(str(row['id']) for row in similar)}."
        approval = _new_approval(
            db, "confirm_expense", summary, now=now, record_id=expense_id,
            expense_hash=payload_hash(expense), similar_ids=[row["id"] for row in similar],
        )
        return _ticket(approval, possible_duplicates=similar)


def _similar_expenses(db: Connection, expense: dict) -> list[dict]:
    return [dict(row) for row in db.execute(
        """SELECT id,job_id,status FROM expenses WHERE vendor_key=? AND date=? AND amount_minor=? AND id<>?
           ORDER BY id""",
        (expense["vendor_key"], expense["date"], expense["amount_minor"], expense["id"]),
    )]


def correct_expense(
    expense_id: int, *, actor: str, vendor: str, payment_date: str, amount: str | int | float,
) -> dict:
    """Apply explicit corrections from the owner's CLI to a proposed receipt."""
    if actor != "cli":
        raise PermissionError("Correct receipt fields from the Hermes CLI.")
    with connect(write=True) as db:
        expense = row_for(db, "expenses", expense_id)
        if expense["status"] != "proposed":
            raise ValueError("Only proposed expenses can be corrected.")
        vendor = text_field(vendor, "vendor", limit=200)
        vendor_key = " ".join(vendor.casefold().split())
        amount_minor = money.minor(amount, expense["currency"])
        if amount_minor <= 0:
            raise ValueError("Expense amount must be positive.")
        db.execute(
            """UPDATE expenses SET vendor=?,vendor_key=?,date=?,amount_minor=?,confidence='high' WHERE id=?""",
            (vendor, vendor_key, date_string(payment_date), amount_minor, expense_id),
        )
        _invalidate_pending(db, "confirm_expense", expense_id)
        event(db, "corrected", "expense", expense_id, actor=actor)
        return row_for(db, "expenses", expense_id)


def request_payment(
    invoice: int | str, amount: str | int | float, *, actor: str, date_paid: str | None = None,
    method: str = "", reference: str = "", request_key: str | None = None, now: datetime | None = None,
) -> dict:
    """Propose a payment from the owner's text for explicit owner confirmation."""
    stamp = timestamp(now)
    with connect(write=True) as db:
        require_owner(db, actor)
        record = row_for(db, "invoices", invoice)
        settings = get_settings(db)
        today = datetime.fromisoformat(stamp).astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
        payment_date = date_string(date_paid) if date_paid else today
        if payment_date > today:
            raise ValueError("Payment date cannot be in the future.")
        method = text_field(method, "payment method", optional=True, limit=100)
        reference = text_field(reference, "payment reference", optional=True, limit=200)
        amount_minor = money.minor(amount, record["currency"])
        paid = db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM payments WHERE invoice_id=?", (record["id"],)).fetchone()[0]
        if request_key and db.execute("SELECT 1 FROM payments WHERE request_key=?", (request_key,)).fetchone():
            raise ValueError("The payment from this message is already recorded.")
        if record["status"] not in ("sent", "on_hold") or not 0 < amount_minor <= record["total_minor"] - paid:
            raise ValueError("Payment must be positive and within the outstanding balance of an issued invoice.")
        summary = f"Record payment on {record['number']}: {record['currency']} {money.major(amount_minor, record['currency'])}"
        if reference:
            summary += f" (reference {reference})"
        fields = {"request_key": request_key, "amount_minor": amount_minor, "payment_date": payment_date,
                  "method": method, "reference": reference, "paid_before": paid}
        if request_key:
            _invalidate_if_changed(db, "payment", record["id"], fields)
        approval = _new_approval(db, "payment", summary, now=now, record_id=record["id"], **fields)
        return _ticket(approval)


def _message_matches(row: dict, payload: dict) -> bool:
    current = message_payload(row)
    if current != payload.get("message") or row["payload_sha256"] != payload_hash(current):
        return False
    try:
        return attachment(row["attachment_path"]) == (row["attachment_path"], row["attachment_sha256"])
    except (OSError, ValueError):
        return False


def _resolve_code(db: Connection, code: str, actor: str, now: datetime | None) -> tuple[dict, dict]:
    require_owner(db, actor)
    row = db.execute("SELECT * FROM approvals WHERE code=?", (code.upper(),)).fetchone()
    if row is None:
        raise ValueError("Unknown approval code.")
    if row["status"] != "pending" or row["used_at"]:
        raise ValueError("This approval code has already been used or invalidated.")
    if row["expires_at"] <= timestamp(now):
        raise ValueError("This approval code has expired.")
    payload = json.loads(row["payload"])
    if payload_hash(payload) != row["payload_sha256"] or payload["owner_binding"] != owner_binding(get_settings(db)):
        raise ValueError("The approval payload or owner route has changed.")
    return dict(row), payload


def _issue_approved(
    db: Connection, kind: str, payload: dict, actor: str, now: datetime | None,
) -> dict:
    record = row_for(db, f"{kind}s", payload["record_id"])
    job = row_for(db, "jobs", record["job_id"])
    if (record["status"] != "awaiting_approval" or _document_hash(record) != payload["document_hash"]
            or _recipient(db, job["id"]) != payload["recipient"]):
        raise ValueError("This document approval is stale.")
    try:
        verified = attachment(payload["attachment_path"])
    except (OSError, ValueError) as exc:
        raise ValueError("The document attachment is unavailable.") from exc
    if verified != (payload["attachment_path"], payload["attachment_sha256"]):
        raise ValueError("The document attachment has changed.")
    if kind == "quote":
        today = datetime.fromisoformat(timestamp(now)).astimezone(ZoneInfo(get_settings(db)["timezone"])).date().isoformat()
        if record["valid_until"] < today or job["state"] not in ("enquiry", "quoted"):
            raise ValueError("This quote cannot be issued.")
        if job["state"] == "enquiry":
            _job_state(db, job["id"], "quoted", actor, now)
    else:
        _job_state(db, job["id"], "invoiced", actor, now)
    transition(db, f"{kind}s", record["id"], "sent", issued_at=timestamp(now))
    event(db, "issued", kind, record["id"], actor=actor, now=now)
    return {"number": record["number"], "attachment_path": payload["attachment_path"]}



def approve(code: str, *, actor: str, now: datetime | None = None) -> dict:
    """Consume an exact-payload code once and commit its validated action."""
    with connect(write=True) as db:
        approval, payload = _resolve_code(db, code, actor, now)
        action = approval["action"]
        outbox_id = payload.get("outbox_id")
        if outbox_id:
            row = row_for(db, "outbox", outbox_id)
            if (row["approval_id"] != approval["id"] or row["status"] != "awaiting_approval"
                    or not _message_matches(row, payload) or not _outbox_live(db, row, now)):
                raise ValueError("This message approval is stale or its payload has changed.")
        result = {}
        if action.startswith("issue_"):
            result = _issue_approved(db, action.removeprefix("issue_"), payload, actor, now)
        elif action == "template":
            settings = get_settings(db)
            entry = settings["templates"].get(payload["kind"])
            if not entry or entry["text"] != payload["text"]:
                raise ValueError("The template has changed.")
            entry["approved_hash"] = payload_hash(payload["text"])
            db.execute("UPDATE settings SET data=? WHERE id=1", (dumps(settings),))
            event(db, "approved", "settings", 1, actor=actor, now=now, template=payload["kind"])
        elif action == "schedule":
            job = row_for(db, "jobs", payload["record_id"])
            active = db.execute("SELECT * FROM appointments WHERE job_id=? AND status='scheduled'", (job["id"],)).fetchone()
            active_hash = payload_hash(dict(active)) if active else None
            if payload_hash(job) != payload["job_hash"] or active_hash != payload["appointment_hash"]:
                raise ValueError("The job or its appointment has changed. Review the booking again.")
            appointment = _schedule(db, job["id"], payload["start"], payload["end"], actor, now)
            result = {"appointment_id": appointment["id"]}
        elif action == "complete":
            job = row_for(db, "jobs", payload["record_id"])
            if payload_hash(job) != payload["job_hash"] or _pricing_binding(get_settings(db)) != payload["pricing_hash"]:
                raise ValueError("The job or pricing has changed. Review completion again.")
            completed = _complete_job(db, job["id"], actor, payload["items"], now)
            result = {"job_number": completed["number"]}
        elif action == "payment":
            invoice = row_for(db, "invoices", payload["record_id"])
            paid = db.execute(
                "SELECT COALESCE(SUM(amount_minor),0) FROM payments WHERE invoice_id=?", (invoice["id"],),
            ).fetchone()[0]
            if paid != payload["paid_before"]:
                raise ValueError("The balance has changed. Review the payment again.")
            key = payload["request_key"]
            if key and db.execute("SELECT 1 FROM payments WHERE request_key=?", (key,)).fetchone():
                raise ValueError("The payment from this message is already recorded.")
            payment = _record_payment(
                db, invoice, payload["amount_minor"], payload["payment_date"], payload["method"],
                payload["reference"], actor, now, key,
            )
            result = {"payment_id": payment["id"], "balance_minor": payment["balance_minor"]}
        elif action == "quote_decision":
            quote = row_for(db, "quotes", payload["record_id"])
            if _document_hash(quote) != payload["document_hash"]:
                raise ValueError("The quote decision is stale.")
            _record_quote_decision(db, quote, payload["decision"], actor, now)
        elif action == "extra_decision":
            extra = row_for(db, "variations", payload["record_id"])
            evidence = row_for(db, "sources", payload["source_id"])
            if (payload_hash(extra) != payload["extra_hash"] or payload_hash(evidence) != payload["evidence_hash"]
                    or (evidence["file_path"] and file_digest(evidence["file_path"]) != evidence["sha256"])):
                raise ValueError("The extra or its evidence has changed.")
            _record_extra_decision(db, extra["id"], evidence["id"], payload["decision"], actor, now)
        elif action == "confirm_expense":
            expense = row_for(db, "expenses", payload["record_id"])
            if (payload_hash(expense) != payload["expense_hash"]
                    or file_digest(expense["receipt_path"]) != expense["receipt_sha256"]
                    or [row["id"] for row in _similar_expenses(db, expense)] != payload["similar_ids"]):
                raise ValueError("The receipt or its possible duplicates have changed. Review the proposal again.")
            _confirm_expense(db, expense["id"], actor, now)
        elif action != "send" or not outbox_id:
            raise ValueError("Unsupported approval action.")
        db.execute("UPDATE approvals SET status='approved',used_at=?,approved_by=? WHERE id=?",
                   (timestamp(now), actor, approval["id"]))
        if outbox_id:
            db.execute("UPDATE outbox SET status='approved' WHERE id=?", (outbox_id,))
        event(db, "approved", "approval", approval["id"], actor=actor, now=now, action=action)
        return {"code": approval["code"], "status": "approved", "outbox_id": outbox_id,
                "summary": payload["summary"], "recipient": payload.get("recipient", (payload.get("message") or {}).get("recipient")), **result}


def reject(code: str, *, actor: str, now: datetime | None = None) -> dict:
    """Reject a code and cancel its pending action."""
    with connect(write=True) as db:
        approval, payload = _resolve_code(db, code, actor, now)
        action = approval["action"]
        outbox_id = payload.get("outbox_id")
        db.execute("UPDATE approvals SET status='rejected',used_at=?,approved_by=? WHERE id=?",
                   (timestamp(now), actor, approval["id"]))
        if action.startswith("issue_"):
            table = action.removeprefix("issue_") + "s"
            record = row_for(db, table, payload["record_id"])
            if record["status"] == "awaiting_approval":
                transition(db, table, record["id"], "draft")
        if outbox_id:
            db.execute(
                """UPDATE outbox SET status='cancelled' WHERE id=? AND approval_id=?
                   AND status='awaiting_approval'""",
                (outbox_id, approval["id"]),
            )
        event(db, "rejected", "approval", approval["id"], actor=actor, now=now, action=action)
        return {"code": approval["code"], "status": "rejected", "outbox_id": outbox_id, "summary": payload["summary"]}


def claim_outbox(outbox_id: int, *, now: datetime | None = None) -> dict | None:
    """Revalidate and atomically claim an approved payload for its only send attempt."""
    with connect(write=True) as db:
        row = row_for(db, "outbox", outbox_id)
        if row["status"] != "approved":
            return None
        approved = row["kind"] == "owner_notice" or bool(row["standing_hash"])
        if row["approval_id"] is not None:
            approval = row_for(db, "approvals", row["approval_id"])
            payload = json.loads(approval["payload"])
            approved = (approval["status"] == "approved" and bool(approval["used_at"])
                        and payload["outbox_id"] == row["id"] and payload_hash(payload) == approval["payload_sha256"]
                        and payload["owner_binding"] == owner_binding(get_settings(db))
                        and _message_matches(row, payload))
        if (not approved or row["payload_sha256"] != payload_hash(message_payload(row))
                or not _outbox_live(db, row, now, for_claim=True)):
            db.execute("UPDATE outbox SET status='cancelled',finished_at=?,detail=? WHERE id=?",
                       (timestamp(now), "Approval or related record is no longer current.", outbox_id))
            event(db, "cancelled", "outbox", outbox_id, now=now)
            _notify_delivery(db, row, "cancelled", "Approval or related record is no longer current.", now)
            return None
        stamp = timestamp(now)
        db.execute("UPDATE outbox SET status='sending',claimed_at=? WHERE id=?", (stamp, outbox_id))
        event(db, "claimed", "outbox", outbox_id, now=now)
        return row | {"status": "sending", "claimed_at": stamp}


def finish_outbox(
    outbox_id: int, outcome: str, *, detail: str = "", now: datetime | None = None,
) -> dict:
    """Record sent, failed or unknown for one claimed send."""
    if outcome not in ("sent", "failed", "unknown"):
        raise ValueError("Invalid delivery outcome.")
    detail = text_field(detail, "delivery detail", optional=True, limit=2000)
    with connect(write=True) as db:
        row = row_for(db, "outbox", outbox_id)
        if row["status"] != "sending":
            if row["status"] == outcome:
                return row
            raise ValueError("Outbox row is not claimed.")
        return _finish_outbox(db, row, outcome, detail, now)


def _finish_outbox(db: Connection, row: dict, outcome: str, detail: str, now: datetime | None) -> dict:
    stamp = timestamp(now)
    db.execute("UPDATE outbox SET status=?,finished_at=?,detail=? WHERE id=?",
               (outcome, stamp, detail, row["id"]))
    if row["kind"] == "invoice":
        db.execute("UPDATE invoices SET delivery_status=? WHERE id=?", (outcome, row["related_id"]))
    event(db, outcome, "outbox", row["id"], now=now, detail=detail)
    notice_id = _notify_delivery(db, row, outcome, detail, now)
    return row | {"owner_notice_id": notice_id} | {"status": outcome, "finished_at": stamp, "detail": detail}


def _notify_delivery(
    db: Connection, row: dict, outcome: str, detail: str, now: datetime | None,
) -> int | None:
    if row["kind"] == "owner_notice":
        return None
    owners = get_settings(db)["owner_phones"]
    if not owners:
        return None
    stage_key = f"delivery-result:{row['id']}:{outcome}"
    prior = _get_stage(db, stage_key)
    if prior:
        return prior["id"]
    label = row["kind"].replace("_", " ")
    if row["kind"] in ("quote", "invoice"):
        label = row_for(db, f"{row['kind']}s", row["related_id"])["number"]
    body = f"Delivery {outcome}: {label} to {row['recipient']}."
    if detail:
        body += f" {display_text(detail)}"
    notice = _store_outbox(db, "owner_notice", owners[0], body, stage_key,
                           job_id=None, related_id=None, status="approved", now=now)
    return notice["id"]

def recover_interrupted_sends(now: datetime | None = None) -> int:
    """Mark claims abandoned for five minutes as unknown so they cannot be resent."""
    cutoff = timestamp(datetime.fromisoformat(timestamp(now)) - timedelta(minutes=5))
    with connect(write=True) as db:
        rows = db.execute("SELECT * FROM outbox WHERE status='sending' AND claimed_at <= ?", (cutoff,)).fetchall()
        for row in rows:
            _finish_outbox(db, dict(row), "unknown", "Delivery stopped before a result was recorded.", now)
        return len(rows)



def _pricing_binding(settings: dict) -> str:
    return payload_hash({key: settings[key] for key in (
        "currency", "hourly_rate_minor", "material_billing_rule", "material_markup_pct", "tax_rate",
    )})


def request_schedule(
    job: int | str, start: str | datetime, end: str | datetime, *, actor: str,
    now: datetime | None = None,
) -> dict:
    """Propose an exact booking or reschedule for explicit owner approval."""
    with connect(write=True) as db:
        require_owner(db, actor)
        selected = row_for(db, "jobs", job)
        settings = get_settings(db)
        if selected["state"] not in ("accepted", "scheduled"):
            raise ValueError("Accept the quote before scheduling this job.")
        start_utc = parse_instant(start, settings["timezone"])
        end_utc = parse_instant(end, settings["timezone"])
        if not timestamp(now) < start_utc < end_utc:
            raise ValueError("Schedule a future start and an end after the start.")
        active = db.execute("SELECT * FROM appointments WHERE job_id=? AND status='scheduled'", (selected["id"],)).fetchone()
        if active and (active["start"], active["end"]) == (start_utc, end_utc):
            return {"status": "scheduled", "appointment": dict(active)}
        fields = {"start": start_utc, "end": end_utc}
        _invalidate_if_changed(db, "schedule", selected["id"], fields)
        local_start = datetime.fromisoformat(start_utc).astimezone(ZoneInfo(settings["timezone"]))
        local_end = datetime.fromisoformat(end_utc).astimezone(ZoneInfo(settings["timezone"]))
        approval = _new_approval(
            db, "schedule", f"Book {selected['number']} from {local_start.isoformat()} to {local_end.isoformat()}",
            now=now, record_id=selected["id"], job_hash=payload_hash(selected),
            appointment_hash=payload_hash(dict(active)) if active else None, **fields,
        )
        return _ticket(approval)


def request_completion(
    job: int | str, *, actor: str, items: list[dict] | None = None, now: datetime | None = None,
) -> dict:
    """Propose completion and any completion-priced lines for owner approval."""
    with connect(write=True) as db:
        require_owner(db, actor)
        selected = row_for(db, "jobs", job)
        if selected["state"] == "done" and items is None:
            return {"status": "done", "job_number": selected["number"]}
        if selected["state"] not in ("enquiry", "accepted", "scheduled"):
            raise ValueError("Only open, uninvoiced work can be completed.")
        accepted = db.execute("SELECT id FROM quotes WHERE job_id=? AND status='accepted'", (selected["id"],)).fetchone()
        if accepted and items is not None:
            raise ValueError("The accepted quote supplies the invoice lines. Record extras separately.")
        settings = get_settings(db)
        lines = price_lines(db, selected["id"], items, settings) if items is not None else None
        _invalidate_if_changed(db, "complete", selected["id"], {"items": items})
        summary = f"Mark {selected['number']} completed"
        if lines:
            amounts = money.totals(lines, Decimal(settings["tax_rate"]))
            summary += f", completion total {settings['currency']} {money.major(amounts['total_minor'], settings['currency'])}"
        approval = _new_approval(
            db, "complete", summary, now=now, record_id=selected["id"],
            job_hash=payload_hash(selected), pricing_hash=_pricing_binding(settings), items=items, lines=lines,
        )
        return _ticket(approval, lines=lines)



def export_books(from_date: str, to_date: str) -> dict:
    """Create the accountant archive behind the service boundary."""
    from .export import export_books as write_archive

    return write_archive(from_date, to_date)


