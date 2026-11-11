from datetime import time
from zoneinfo import ZoneInfo

from ..core import queries, service
from .text import customer_text


SECTIONS = (
    ("Pending approvals", "pending_approvals", ("code", "summary", "recipient")),
    ("Quotes waiting over 3 days", "aging_quotes", ("number", "customer_name", "sent_at")),
    ("Overdue invoices", "overdue_invoices", ("number", "customer_name", "due_at")),
    ("Unconfirmed receipts", "unconfirmed_receipts", ("vendor", "date", "id")),
    ("Today and tomorrow's appointments", "appointments", ("job_number", "customer_name", "start_at", "start")),
    ("Delivery problems", "delivery_problems", ("id", "kind", "recipient", "status", "detail")),
)


def build_digest(now=None):
    now = now or queries.clock_now()
    data = queries.digest(now)
    lines = ["Chalkline daily digest"]
    for title, key, fields in SECTIONS:
        rows = data.get(key, [])
        lines.append(f"\n{title} ({len(rows)})")
        section = []
        for row in rows:
            parts = [str(row[field]) for field in fields if row.get(field) not in (None, "")]
            item = " · ".join(parts or [str(row["id"])])
            line = "• " + (item if len(item) <= 220 else item[:219] + "…")
            if sum(len(entry) + 1 for entry in section) + len(line) > 540:
                section.append(f"… {len(rows) - len(section)} more")
                break
            section.append(line)
        lines.extend(section)
    return customer_text("\n".join(lines))


def queue_digest(now):
    settings = queries.settings()
    owners = settings.get("owner_phones") or []
    digest_time = settings.get("digest_time")
    if not owners or not digest_time:
        return None
    local_now = now.astimezone(ZoneInfo(settings.get("timezone") or "UTC"))
    if local_now.time() < time.fromisoformat(digest_time):
        return None
    return service.enqueue_owner_notice(
        owners[0], build_digest(now), f"daily-digest:{local_now.date().isoformat()}", now=now,
    )
