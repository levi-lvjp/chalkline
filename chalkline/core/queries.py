"""JSON-ready reads for channels, commands and the dashboard."""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from plugins.plugin_storage import plugin_data_dir

from .. import SLUG

from .approvals import owner_binding, payload_hash
from .files import file_digest
from .records import current_time, get_settings, row_for, timestamp
from .schema import connect


def settings() -> dict:
    """Return settings including owner_phones, timezone, digest_time and templates."""
    with connect() as db:
        return get_settings(db)


def is_owner(phone: str) -> bool:
    """Return whether the E.164 WhatsApp sender is configured as an owner."""
    return phone in settings()["owner_phones"]


def _decode(row: dict, *fields: str) -> dict:
    return row | {key: json.loads(row[key]) for key in fields if row[key] is not None}


def jobs(state: str | None = None) -> list[dict]:
    """Return job summaries, optionally filtered to one state."""
    with connect() as db:
        return [dict(row) for row in db.execute(
            """SELECT j.id, j.number, j.description, j.site_address, j.state, j.updated_at,
                      c.name AS customer_name
               FROM jobs j JOIN customers c ON c.id=j.customer_id
               WHERE (? IS NULL OR j.state=?) ORDER BY j.id DESC""",
            (state, state),
        )]


def job_detail(job: int | str) -> dict:
    """Return a job, customer and attached ledger records by ID or job number."""
    with connect() as db:
        selected = _decode(row_for(db, "jobs", job), "completion_lines")
        related = {}
        for table in ("sources", "quotes", "variations", "appointments", "invoices", "expenses"):
            rows = [dict(row) for row in db.execute(
                f"SELECT * FROM {table} WHERE job_id = ? ORDER BY id", (selected["id"],),
            )]
            related[table] = [_decode(row, "lines", "document") for row in rows] if table in ("quotes", "invoices") else rows
        for source in related["sources"]:
            source["trust"] = "evidence_only"
        related["payments"] = [dict(row) for row in db.execute(
            """SELECT p.* FROM payments p JOIN invoices i ON p.invoice_id=i.id
               WHERE i.job_id = ? ORDER BY p.id""", (selected["id"],),
        )]
        return {"job": selected, "customer": row_for(db, "customers", selected["customer_id"])} | related


def pipeline() -> dict[str, int]:
    """Return job counts for every lifecycle state."""
    from .states import TRANSITIONS

    with connect() as db:
        counts = {state: 0 for state in TRANSITIONS["jobs"]}
        counts.update({row["state"]: row["count"] for row in db.execute(
            "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state",
        )})
        return counts


def money_owed() -> list[dict]:
    """Return issued, unpaid invoices with customer and exact outstanding balance."""
    with connect() as db:
        return _money_owed(db)


def _money_owed(db) -> list[dict]:
    return [dict(row) for row in db.execute(
        """SELECT i.id, i.number, i.status, i.delivery_status, i.currency, i.issued_at, i.due_at,
                  i.total_minor, i.job_id, j.number AS job_number, c.name AS customer_name, c.phone,
                  i.total_minor - COALESCE(SUM(p.amount_minor),0) AS balance_minor
           FROM invoices i JOIN jobs j ON j.id=i.job_id JOIN customers c ON c.id=j.customer_id
           LEFT JOIN payments p ON p.invoice_id=i.id
           WHERE i.status IN ('sent','on_hold') GROUP BY i.id ORDER BY i.due_at, i.id""",
    )]


def expenses() -> list[dict]:
    """Return expense proposals and confirmed expense records."""
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM expenses ORDER BY date DESC, id DESC")]


def pending_approvals(now: datetime | None = None) -> list[dict]:
    """Return actionable codes and the exact payload preview the owner is reviewing."""
    with connect() as db:
        return _pending_approvals(db, now)


def _pending_approvals(db, now: datetime | None) -> list[dict]:
    binding = owner_binding(get_settings(db))
    result = []
    for row in db.execute(
        """SELECT id,code,action,payload,payload_sha256,created_at,expires_at FROM approvals
           WHERE status='pending' AND expires_at > ? ORDER BY id""",
        (timestamp(now),),
    ):
        payload = json.loads(row["payload"])
        if payload["owner_binding"] != binding or payload_hash(payload) != row["payload_sha256"]:
            continue
        message = payload.get("message") or {}
        result.append({
            "id": row["id"], "code": row["code"], "action": row["action"],
            "summary": payload["summary"], "kind": message.get("kind", row["action"]),
            "recipient": message.get("recipient"), "body": message.get("body", payload.get("text")),
            "attachment_path": payload.get("attachment_path", message.get("attachment_path")),
            "details": {key: payload[key] for key in ("record_id", "source_id", "decision", "lines", "start", "end",
                        "amount_minor", "payment_date", "method", "reference") if key in payload},
            "created_at": row["created_at"], "expires_at": row["expires_at"],
        })
    return result

def clock_now() -> datetime:
    """Return the shared real or explicitly selected demo clock."""
    return current_time()


def due_actions(now: datetime | None = None) -> list[dict]:
    """Return unqueued current-stage enqueue_message keyword arguments."""
    from .followups import appointment_reminder, payment_chaser, quote_followup, review_request

    instant = datetime.fromisoformat(timestamp(now))
    with connect() as db:
        local = instant.astimezone(ZoneInfo(get_settings(db)["timezone"]))
        queued = {row[0] for row in db.execute("SELECT stage_key FROM outbox")}
        delivered = {(row["kind"], row["related_id"]) for row in db.execute(
            "SELECT kind,related_id FROM outbox WHERE status='sent' AND kind IN ('quote','invoice')")}
        quotes = [dict(row) for row in db.execute(
            """SELECT q.*, c.phone FROM quotes q JOIN jobs j ON j.id=q.job_id
               JOIN customers c ON c.id=j.customer_id
               WHERE q.status='sent' AND j.state='quoted'
               AND NOT EXISTS (SELECT 1 FROM quotes newer WHERE newer.job_id=q.job_id AND newer.revision>q.revision)
               ORDER BY q.id"""
        )]
        appointments = [dict(row) for row in db.execute(
            """SELECT a.*, c.phone FROM appointments a JOIN jobs j ON j.id=a.job_id
               JOIN customers c ON c.id=j.customer_id
               WHERE a.status='scheduled' AND j.state='scheduled' ORDER BY a.start,a.id"""
        )]
        paid_jobs = [dict(row) for row in db.execute(
            """SELECT j.*, c.phone FROM jobs j JOIN customers c ON c.id=j.customer_id
               WHERE j.state='paid' ORDER BY j.id"""
        )]
        actions = (
            [quote_followup(row, instant, local.date()) for row in quotes if ("quote", row["id"]) in delivered]
            + [payment_chaser(row, local.date()) for row in _money_owed(db) if ("invoice", row["id"]) in delivered]
            + [appointment_reminder(row, instant) for row in appointments]
            + [review_request(row) for row in paid_jobs if row["updated_at"] > get_settings(db).get("setup_at", timestamp(instant))]
        )
        return [action for action in actions if action is not None and action["stage_key"] not in queued]


def digest(now: datetime | None = None) -> dict:
    """Read pending work, overdue balances and today's and tomorrow's appointments."""
    stamp = timestamp(now)
    instant = datetime.fromisoformat(stamp)
    with connect() as db:
        zone = ZoneInfo(get_settings(db)["timezone"])
        today = instant.astimezone(zone).date()
        start = timestamp(datetime.combine(today, time.min, zone))
        end = timestamp(datetime.combine(today + timedelta(days=1), time.max, zone))
        aging = [dict(row) for row in db.execute(
            """SELECT q.id,q.number,q.revision,q.issued_at AS sent_at,c.name AS customer_name
               FROM quotes q JOIN jobs j ON j.id=q.job_id JOIN customers c ON c.id=j.customer_id
               WHERE q.status='sent' AND q.issued_at<=? AND q.valid_until>=? ORDER BY q.issued_at,q.id""",
            (timestamp(instant - timedelta(days=3)), today.isoformat()),
        )]
        overdue = [row for row in _money_owed(db) if row["due_at"] < today.isoformat() and row["balance_minor"] > 0]
        receipts = [dict(row) for row in db.execute(
            "SELECT id,vendor,date,amount_minor,currency,job_id,confidence FROM expenses WHERE status='proposed' ORDER BY id"
        )]
        appointments = [dict(row) for row in db.execute(
            """SELECT a.id,a.start,a.end,j.number AS job_number,c.name AS customer_name
               FROM appointments a JOIN jobs j ON j.id=a.job_id JOIN customers c ON c.id=j.customer_id
               WHERE a.status='scheduled' AND a.start BETWEEN ? AND ? ORDER BY a.start,a.id""",
            (start, end),
        )]
        problems = [dict(row) for row in db.execute(
            """SELECT id,kind,recipient,status,detail,finished_at FROM outbox
               WHERE kind<>'owner_notice' AND status IN ('failed','unknown','cancelled')
               ORDER BY id DESC LIMIT 20"""
        )]
        return {"pending_approvals": _pending_approvals(db, instant), "aging_quotes": aging,
                "overdue_invoices": overdue, "unconfirmed_receipts": receipts, "appointments": appointments,
                "delivery_problems": problems}

def get_outbox(outbox_id: int) -> dict:
    """Return an outbox row by ID."""
    with connect() as db:
        return row_for(db, "outbox", outbox_id)


def ready_outbox() -> list[dict]:
    """Return approved rows eligible for a claim attempt."""
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM outbox WHERE status='approved' ORDER BY id")]


def export_rows(start: str, end: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Read all dated accountant records from a single ledger snapshot."""
    with connect() as db:
        zone = ZoneInfo(get_settings(db)["timezone"])
        try:
            start_stamp = timestamp(datetime.combine(date.fromisoformat(start), time.min, zone))
            end_stamp = timestamp(datetime.combine(date.fromisoformat(end), time.max, zone))
        except OverflowError as exc:
            raise ValueError("Export date range is outside the supported calendar.") from exc
        invoices = [dict(row) for row in db.execute(
            """SELECT i.*, j.number AS job, c.name AS customer
               FROM invoices i JOIN jobs j ON j.id=i.job_id JOIN customers c ON c.id=j.customer_id
               WHERE i.issued_at BETWEEN ? AND ? ORDER BY i.issued_at, i.id""",
            (start_stamp, end_stamp),
        )]
        for row in invoices:
            row["date"] = datetime.fromisoformat(row["issued_at"]).astimezone(zone).date().isoformat()
        payments = [dict(row) for row in db.execute(
            """SELECT p.*, i.number, i.currency, j.number AS job, c.name AS customer
               FROM payments p JOIN invoices i ON i.id=p.invoice_id JOIN jobs j ON j.id=i.job_id
               JOIN customers c ON c.id=j.customer_id
               WHERE p.date BETWEEN ? AND ? ORDER BY p.date, p.id""",
            (start, end),
        )]
        expenses = [dict(row) for row in db.execute(
            """SELECT e.*, j.number AS job FROM expenses e LEFT JOIN jobs j ON j.id=e.job_id
               WHERE e.status='confirmed' AND e.date BETWEEN ? AND ? ORDER BY e.id""",
            (start, end),
        )]
        return invoices, payments, expenses


def source_file(source_id: int) -> Path:
    """Return a verified profile-local source for dashboard viewing."""
    with connect() as db:
        row = row_for(db, "sources", source_id)
    if not row["file_path"]:
        raise ValueError("Source has no file.")
    path = Path(row["file_path"]).resolve()
    root = plugin_data_dir(SLUG).resolve() / "sources"
    if not path.is_relative_to(root) or file_digest(path) != row["sha256"]:
        raise ValueError("Source file has changed.")
    return path


def delivery_notices(outbox_id: int) -> list[dict]:
    """Return queued owner result notices for a customer send."""
    with connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM outbox WHERE stage_key LIKE ? AND status='approved' ORDER BY id",
            (f"delivery-result:{outbox_id}:%",),
        )]

def job_events(job: int | str) -> list[dict]:
    """Return the append-only audit timeline for a job and its related records."""
    with connect() as db:
        selected = row_for(db, "jobs", job)
        ids = {"job": [selected["id"]]}
        for entity, table in (
            ("source", "sources"), ("quote", "quotes"), ("invoice", "invoices"),
            ("variation", "variations"), ("expense", "expenses"), ("appointment", "appointments"),
            ("outbox", "outbox"),
        ):
            ids[entity] = [row[0] for row in db.execute(f"SELECT id FROM {table} WHERE job_id=?", (selected["id"],))]
        ids["payment"] = [row[0] for row in db.execute(
            "SELECT p.id FROM payments p JOIN invoices i ON i.id=p.invoice_id WHERE i.job_id=?", (selected["id"],),
        )]
        ids["approval"] = []
        action_entities = {"issue_quote": "quote", "quote_decision": "quote", "issue_invoice": "invoice",
                           "payment": "invoice", "confirm_expense": "expense", "extra_decision": "variation",
                           "schedule": "job", "complete": "job"}
        for row in db.execute("SELECT id,action,payload FROM approvals"):
            payload = json.loads(row["payload"])
            entity = action_entities.get(row["action"])
            if (payload.get("outbox_id") in ids["outbox"]
                    or entity is not None and payload.get("record_id") in ids[entity]):
                ids["approval"].append(row["id"])
        clauses = []
        values = []
        for entity, references in ids.items():
            if references:
                clauses.append(f"(entity=? AND entity_id IN ({','.join('?' for _ in references)}))")
                values.extend((entity, *references))
        rows = db.execute("SELECT * FROM events WHERE " + " OR ".join(clauses) + " ORDER BY id", values)
        return [dict(row) | {"data": json.loads(row["data"])} for row in rows]