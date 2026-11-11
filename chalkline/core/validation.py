"""Validate fields entering the office ledger."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import money
from .records import phone_number, timestamp


TEMPLATES = {
    "enquiry_ack": frozenset({"business_name", "customer_name", "job_number"}),
    "appointment_reminder": frozenset({"business_name", "customer_name", "job_number", "appointment_start"}),
    "review_request": frozenset({"business_name", "customer_name", "job_number", "review_link"}),
}


def text_field(value: str, label: str, *, optional: bool = False, limit: int = 2000) -> str:
    """Validate a bounded human-entered string."""
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ValueError(f"Provide {label} of at most {limit} characters.")
    return value.strip()


def parse_instant(value: str | datetime, timezone: str) -> str:
    """Convert an explicit instant, or an unambiguous business-local time, to UTC."""
    from datetime import timezone as utc_timezone

    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(instant, datetime):
        raise ValueError("Use an ISO 8601 date and time.")
    if instant.utcoffset() is None:
        zone = ZoneInfo(timezone)
        first = instant.replace(tzinfo=zone, fold=0)
        second = instant.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("This local time is ambiguous or invalid. Provide the UTC offset.")
        if first.astimezone(utc_timezone.utc).astimezone(zone).replace(tzinfo=None) != instant:
            raise ValueError("This local time does not exist.")
        instant = first
    return timestamp(instant)


def setup_values(current: dict, changes: dict) -> dict:
    """Normalize editable business settings while preserving template approvals."""
    editable = {
        "business_name", "owner_phones", "currency", "locale", "timezone", "hourly_rate",
        "material_billing_rule", "material_markup_pct", "tax_rate", "payment_terms_days",
        "payment_text", "review_link", "digest_time", "telegram_owner_user_id", "telegram_owner_chat_id",
    }
    unknown = changes.keys() - editable
    if unknown:
        raise ValueError(f"Unknown setup fields: {', '.join(sorted(unknown))}.")
    result = current | {key: value for key, value in changes.items() if key != "hourly_rate"}
    for key in ("business_name", "locale", "timezone"):
        result[key] = text_field(result[key], key, limit=100)
    for key in ("payment_text", "review_link"):
        result[key] = text_field(result[key], key, optional=True)
    try:
        ZoneInfo(result["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Use an IANA timezone such as Europe/London.") from exc
    if not isinstance(result["owner_phones"], list) or not result["owner_phones"]:
        raise ValueError("Provide at least one owner phone in owner_phones.")
    result["owner_phones"] = list(dict.fromkeys(phone_number(number) for number in result["owner_phones"]))
    currency = text_field(result["currency"], "currency", limit=3).upper()
    money.currency_scale(currency)
    if currency != current["currency"] and "hourly_rate" not in changes and current["hourly_rate_minor"]:
        raise ValueError("Supply the hourly rate in the new currency.")
    result["currency"] = currency
    if "hourly_rate" in changes:
        result["hourly_rate_minor"] = money.minor(changes["hourly_rate"], currency)
    if result["hourly_rate_minor"] < 0:
        raise ValueError("Hourly rate must be nonnegative.")
    if result["material_billing_rule"] not in ("cost", "cost_plus_pct"):
        raise ValueError("Material billing rule must be cost or cost_plus_pct.")
    for key in ("material_markup_pct", "tax_rate"):
        value = money.decimal(result[key])
        if not 0 <= value <= (100 if key == "tax_rate" else 1000):
            raise ValueError(f"Invalid {key}.")
        result[key] = format(value, "f")
    days = result["payment_terms_days"]
    if type(days) is not int or not 0 <= days <= 365:
        raise ValueError("Payment terms must be a whole number between 0 and 365 days.")
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", str(result["digest_time"])):
        raise ValueError("Digest time must use HH:MM in the business timezone.")
    telegram = [result["telegram_owner_user_id"], result["telegram_owner_chat_id"]]
    if any(value is not None for value in telegram):
        if not all(re.fullmatch(r"[1-9][0-9]*", str(value)) for value in telegram) or str(telegram[0]) != str(telegram[1]):
            raise ValueError("Telegram owner user and private chat IDs must match.")
        result["telegram_owner_user_id"], result["telegram_owner_chat_id"] = map(str, telegram)
    return result
