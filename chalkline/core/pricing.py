"""Validate priced lines and apply the business's labour and material rules."""

from decimal import Decimal, localcontext
from sqlite3 import Connection

from . import money


def price_lines(db: Connection, job_id: int, items: list[dict], settings: dict) -> list[dict]:
    """Produce exact priced lines from quantities, costs and confirmed receipts."""
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("Provide between 1 and 100 line items.")
    currency = settings["currency"]
    priced = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each line item must be an object.")
        description = item.get("description")
        kind = item.get("kind", "other")
        quantity = money.decimal(item.get("qty", 1))
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise ValueError("Each line needs a description of at most 500 characters.")
        if kind not in ("labour", "material", "other") or quantity <= 0:
            raise ValueError("Each line needs a supported kind and positive quantity.")
        receipt_id = item.get("expense_id")
        if receipt_id is not None:
            if kind != "material" or quantity != 1:
                raise ValueError("A receipt is a single material line.")
            receipt = db.execute(
                """SELECT amount_minor FROM expenses
                   WHERE id = ? AND job_id = ? AND status = 'confirmed' AND currency = ?""",
                (receipt_id, job_id, currency),
            ).fetchone()
            if receipt is None:
                raise ValueError("Use a confirmed receipt from this job.")
            if any(line.get("expense_id") == receipt_id for line in priced):
                raise ValueError("Use a receipt at most once per document.")
            price = receipt["amount_minor"]
        elif kind == "labour" and item.get("unit_price") is None:
            price = settings["hourly_rate_minor"]
            if price <= 0:
                raise ValueError("Set an hourly rate or provide a labour unit price.")
        else:
            price = money.minor(item.get("unit_price"), currency)
        if price < 0:
            raise ValueError("Unit prices must be nonnegative.")
        if kind == "material" and settings["material_billing_rule"] == "cost_plus_pct":
            with localcontext() as ctx:
                ctx.prec = 50
                price = money.round_minor(Decimal(price) * (1 + Decimal(settings["material_markup_pct"]) / 100))
        line = {
            "description": description.strip(), "kind": kind, "qty": format(quantity, "f"),
            "unit_price_minor": price, "total_minor": money.line_total(quantity, price),
        }
        if receipt_id is not None:
            line["expense_id"] = receipt_id
        priced.append(line)
    return priced
