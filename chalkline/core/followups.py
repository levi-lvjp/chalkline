"""Pure scheduling rules for customer follow-ups and appointment reminders."""

from datetime import date, datetime, timedelta

from .money import major


def quote_followup(quote: dict, now: datetime, today: date) -> dict | None:
    """Propose one follow-up three days after this exact quote revision was issued."""
    if (quote["status"] != "sent" or not quote["phone"] or not quote["issued_at"]
            or quote["valid_until"] < today.isoformat()
            or now < datetime.fromisoformat(quote["issued_at"]) + timedelta(days=3)):
        return None
    return {
        "kind": "quote_followup", "recipient": quote["phone"], "job_id": quote["job_id"],
        "related_id": quote["id"], "stage_key": f"quote_followup:{quote['id']}:day3",
        "body": f"Following up on quote {quote['number']} revision {quote['revision']}. Please let us know if you would like to go ahead.",
    }


def payment_chaser(invoice: dict, today: date) -> dict | None:
    """Propose the latest due chaser at seven, fourteen or thirty days overdue."""
    if invoice["status"] != "sent" or invoice["balance_minor"] <= 0 or not invoice["phone"]:
        return None
    overdue = (today - date.fromisoformat(invoice["due_at"])).days
    stages = [stage for stage in (7, 14, 30) if overdue >= stage]
    if not stages:
        return None
    stage = stages[-1]
    wording = {
        7: "A friendly reminder",
        14: "A payment reminder",
        30: "A final payment reminder",
    }[stage]
    balance = f"{invoice['currency']} {major(invoice['balance_minor'], invoice['currency'])}"
    return {
        "kind": "payment_chaser", "recipient": invoice["phone"], "job_id": invoice["job_id"],
        "related_id": invoice["id"], "stage_key": f"payment_chaser:{invoice['id']}:day{stage}",
        "body": f"{wording}: {balance} remains due on invoice {invoice['number']}, due {invoice['due_at']}. Please let us know when payment has been made.",
    }


def appointment_reminder(appointment: dict, now: datetime) -> dict | None:
    """Return the current reminder window, using a separate stage for each appointment."""
    if appointment["status"] != "scheduled" or not appointment["phone"]:
        return None
    remaining = datetime.fromisoformat(appointment["start"]) - now
    if remaining <= timedelta(0) or remaining > timedelta(hours=24):
        return None
    stage = "1h" if remaining <= timedelta(hours=1) else "24h"
    return {
        "kind": "appointment_reminder", "recipient": appointment["phone"], "job_id": appointment["job_id"],
        "related_id": appointment["id"], "stage_key": f"appointment_reminder:{appointment['id']}:{stage}",
        "standing_template": "appointment_reminder", "body": "",
    }


def review_request(job: dict) -> dict | None:
    """Return one standing review request after full payment."""
    if job["state"] != "paid" or not job["phone"]:
        return None
    return {
        "kind": "review_request", "recipient": job["phone"], "job_id": job["id"],
        "related_id": None, "stage_key": f"review_request:{job['id']}",
        "standing_template": "review_request", "body": "",
    }
