"""Shared record serialization, timestamps and profile settings."""

import json
import os
import re
from datetime import date, datetime, timezone
from sqlite3 import Connection


DEFAULT_SETTINGS = {
    "business_name": "",
    "owner_phones": [],
    "currency": "GBP",
    "locale": "en-GB",
    "timezone": "UTC",
    "hourly_rate_minor": 0,
    "material_billing_rule": "cost",
    "material_markup_pct": "0",
    "tax_rate": "0",
    "payment_terms_days": 30,
    "payment_text": "",
    "review_link": "",
    "digest_time": "17:00",
    "templates": {},
    "telegram_owner_user_id": None,
    "telegram_owner_chat_id": None,
}


def dumps(data: object) -> str:
    """Serialize ledger payloads consistently for storage and hashing."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def current_time() -> datetime:
    """Return UTC now, with an explicit demo clock limited to explicit development profiles."""
    override = os.environ.get("CHALKLINE_TEST_NOW")
    if override:
        from hermes_constants import get_hermes_home

        if get_hermes_home().name not in ("chalkline-dev", "chalkline-ui"):
            raise ValueError("The test clock is available only in chalkline-dev or chalkline-ui.")
        instant = datetime.fromisoformat(override)
        if instant.utcoffset() is None:
            raise ValueError("The test clock requires an explicit UTC offset.")
        return instant.astimezone(timezone.utc)
    return datetime.now(timezone.utc)

def timestamp(now: datetime | None = None) -> str:
    """Normalize a timezone-aware datetime to a sortable UTC timestamp."""
    now = current_time() if now is None else now
    if now.utcoffset() is None:
        raise ValueError("A timezone offset is required.")
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds")


def date_string(value: str) -> str:
    """Validate a calendar date in YYYY-MM-DD form."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use a date in YYYY-MM-DD form.")
    return date.fromisoformat(value).isoformat()


def phone_number(value: str) -> str:
    """Validate an E.164 phone number."""
    if not isinstance(value, str) or not re.fullmatch(r"\+[1-9][0-9]{5,14}", value):
        raise ValueError("Use an E.164 phone number such as +12025550109.")
    return value


def get_settings(db: Connection) -> dict:
    """Load settings from an existing transaction."""
    row = db.execute("SELECT data FROM settings WHERE id = 1").fetchone()
    return DEFAULT_SETTINGS | (json.loads(row[0]) if row else {})


def require_owner(db: Connection, actor: str, *, cli: bool = True) -> None:
    """Verify a transport-provided actor against current owner settings."""
    settings = get_settings(db)
    telegram_user = settings["telegram_owner_user_id"]
    telegram_chat = settings["telegram_owner_chat_id"]
    if (cli and actor in ("cli", "dashboard")) or actor in settings["owner_phones"]:
        return
    if telegram_user and telegram_chat and actor == f"telegram:{telegram_user}:{telegram_chat}":
        return
    raise PermissionError("This action requires the configured owner.")


def event(
    db: Connection, kind: str, entity: str, entity_id: int | None,
    *, actor: str = "system", now: datetime | None = None, **data: object,
) -> None:
    """Append an audit event in the transaction that changes the ledger."""
    db.execute(
        "INSERT INTO events(created_at, kind, entity, entity_id, actor, data) VALUES (?, ?, ?, ?, ?, ?)",
        (timestamp(now), kind, entity, entity_id, actor, dumps(data)),
    )


def row_for(db: Connection, table: str, reference: int | str) -> dict:
    """Resolve a numeric ID or business number within a specific ledger table."""
    field = "id" if type(reference) is int else "number"
    order = " ORDER BY revision DESC" if table == "quotes" else ""
    row = db.execute(f"SELECT * FROM {table} WHERE {field} = ?{order} LIMIT 1", (reference,)).fetchone()
    if row is None:
        raise ValueError(f"No {table} record matches {reference!r}.")
    return dict(row)
