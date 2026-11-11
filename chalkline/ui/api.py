import base64
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core import queries, service


# Hermes mounts this router only under /api/plugins/, after its dashboard auth middleware.
router = APIRouter()


def public(value):
    """Remove filesystem locations from nested ledger projections."""
    if isinstance(value, list):
        return [public(item) for item in value]
    if isinstance(value, dict):
        result = {key: public(item) for key, item in value.items() if key != "path" and not key.endswith("_path")}
        if value.get("attachment_path"):
            result["attachment_name"] = Path(value["attachment_path"]).name
        return result
    return value


def invoke(function, *args, **kwargs):
    try:
        return public(function(*args, **kwargs))
    except (ValueError, PermissionError, OSError) as exc:
        raise HTTPException(409, "The action is unavailable, expired, or has changed. Refresh and review it again.") from exc


@router.get("/overview")
def overview():
    settings = queries.settings()
    now = queries.clock_now().astimezone(ZoneInfo(settings["timezone"]))
    today = now.date()
    month = today.replace(day=1).isoformat()
    _, payments, expenses = queries.export_rows(month, today.isoformat())
    owed = queries.money_owed()
    pipeline = {state: [] for state in queries.pipeline()}
    appointments = []
    amounts = {}
    week = today - timedelta(days=today.weekday())
    for card in queries.jobs():
        detail = queries.job_detail(card["id"])
        documents = detail["invoices"] or detail["quotes"]
        for kind in ("quotes", "invoices"):
            for document in detail[kind]:
                amounts[(kind[:-1], document["id"])] = document["total_minor"]
        latest = documents[-1] if documents else {}
        pipeline[card["state"]].append(public(card | {
            "total_minor": latest.get("total_minor"), "currency": settings["currency"],
        }))
        for appointment in detail["appointments"]:
            local_day = datetime.fromisoformat(appointment["start"]).astimezone(
                ZoneInfo(settings["timezone"])).date()
            if appointment["status"] == "scheduled" and week <= local_day < week + timedelta(days=7):
                appointments.append(appointment | {"job_number": card["number"], "customer_name": card["customer_name"]})
    approvals = public(queries.pending_approvals())
    for approval in approvals:
        record = approval["details"].get("record_id")
        kind = approval["action"].removeprefix("issue_")
        approval["amount_minor"] = approval["details"].get("amount_minor", amounts.get((kind, record)))
        approval["currency"] = settings["currency"]
    return {
        "business_name": settings["business_name"], "currency": settings["currency"],
        "locale": settings["locale"], "timezone": settings["timezone"], "today": today.isoformat(),
        "pipeline": pipeline,
        "money": {
            "owed_minor": sum(row["balance_minor"] for row in owed),
            "overdue_minor": sum(row["balance_minor"] for row in owed if row["due_at"] < today.isoformat()),
            "paid_this_month_minor": sum(row["amount_minor"] for row in payments),
            "expenses_this_month_minor": sum(row["amount_minor"] for row in expenses),
        },
        "invoices": public(owed), "appointments": sorted(appointments, key=lambda row: row["start"]),
        "approvals": approvals,
    }


@router.get("/jobs/{number}")
def job(number: str):
    try:
        detail = queries.job_detail(number)
        for source in detail["sources"]:
            source["thumbnail_url"] = f"/sources/{source['id']}/thumbnail" if source["kind"] in ("photo", "receipt") else None
        detail["timeline"] = queries.job_events(number)
        return public(detail)
    except ValueError as exc:
        raise HTTPException(404, "Job unavailable.") from exc


@router.get("/sources/{source_id}/thumbnail")
def thumbnail(source_id: int):
    from PIL import Image, ImageOps

    try:
        path = queries.source_file(source_id)
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Image too large")
        with Image.open(path) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError("Image too large")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((320, 240))
            output = BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=75)
            return {"data_url": "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()}
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(404, "Thumbnail unavailable.") from exc


class Dates(BaseModel):
    from_date: str = Field(alias="from")
    to_date: str = Field(alias="to")


@router.post("/export")
def export(dates: Dates):
    try:
        generated = service.export_books(dates.from_date, dates.to_date)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, "Export unavailable for this date range.") from exc
    path = Path(generated["path"])
    if path.stat().st_size > 20 * 1024 * 1024:
        raise HTTPException(413, "Export exceeds the demo download limit.")
    return {"filename": path.name, "base64": base64.b64encode(path.read_bytes()).decode()}


@router.post("/approvals/{code}/approve")
def approve(code: str):
    result = invoke(service.approve, code, actor="dashboard")
    if result.get("outbox_id") is not None:
        from ..core.host import optional_channel
        outbound = optional_channel("outbound")
        result["delivery"] = outbound.deliver_outbox(result["outbox_id"]) if outbound else "queued"
    return result


@router.post("/approvals/{code}/reject")
def reject(code: str):
    return invoke(service.reject, code, actor="dashboard")


class Payment(BaseModel):
    amount: str
    date_paid: str | None = None
    method: str = ""
    reference: str = ""


@router.post("/invoices/{number}/payments")
def payment(number: str, values: Payment):
    return invoke(service.request_payment, number, actor="dashboard", **values.model_dump())
