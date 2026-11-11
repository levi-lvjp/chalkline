"""Deterministic accountant export of issued invoices, payments and confirmed expenses."""

import csv
import io
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from plugins.plugin_storage import plugin_data_dir

from .. import NAME, SLUG
from .files import file_digest
from .money import major
from .queries import export_rows
from .records import date_string


INCOME_FIELDS = (
    "type", "date", "invoice", "job", "customer", "status", "currency", "subtotal", "tax", "total",
    "method", "reference",
)
EXPENSE_FIELDS = ("id", "date", "vendor", "category", "job", "currency", "total", "receipt")


def _csv(fields: tuple[str, ...], rows: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        safe = {
            key: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
            for key, value in row.items()
        }
        writer.writerow(safe)
    return stream.getvalue().encode("utf-8-sig")


def _entry(archive: ZipFile, name: str, data: bytes) -> None:
    # ZipInfo defaults to 1980-01-01 and fixed file metadata.
    archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED, compresslevel=6)


def export_books(from_date: str, to_date: str) -> dict:
    """Create a byte-stable archive for an inclusive business date range."""
    start, end = date_string(from_date), date_string(to_date)
    if start > end:
        raise ValueError("Export start date must be on or before the end date.")
    invoices, payments, expenses = export_rows(start, end)
    income = [
        {"type": "invoice", "date": row["date"], "invoice": row["number"], "job": row["job"],
         "customer": row["customer"], "status": row["status"], "currency": row["currency"],
         **{field: major(row[f"{field}_minor"], row["currency"]) for field in ("subtotal", "tax", "total")}}
        for row in invoices
    ]
    income.extend(
        {"type": "payment", "date": row["date"], "invoice": row["number"], "job": row["job"],
         "customer": row["customer"], "currency": row["currency"], "total": major(row["amount_minor"], row["currency"]),
         "method": row["method"], "reference": row["reference"]}
        for row in payments
    )
    income.sort(key=lambda row: (row["date"], row["invoice"], row["type"]))
    directory = plugin_data_dir(SLUG) / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=directory, delete=False) as scratch:
        temporary = Path(scratch.name)
    try:
        with ZipFile(temporary, "w") as archive:
            _entry(archive, "income.csv", _csv(INCOME_FIELDS, income))
            archive.writestr(ZipInfo("receipts/"), b"")
            expense_rows = []
            for row in expenses:
                receipt = Path(row["receipt_path"]).resolve()
                if not receipt.is_relative_to(plugin_data_dir(SLUG).resolve() / "sources"):
                    raise ValueError(f"Receipt {row['id']} is outside the {NAME} data directory.")
                name = f"receipts/receipt-{row['id']:04d}{receipt.suffix.lower()}"
                data = receipt.read_bytes()
                if sha256(data).hexdigest() != row["receipt_sha256"]:
                    raise ValueError(f"Receipt {row['id']} has changed since it was recorded.")
                _entry(archive, name, data)
                expense_rows.append({
                    **{field: row[field] for field in ("id", "date", "vendor", "category", "job", "currency")},
                    "total": major(row["amount_minor"], row["currency"]), "receipt": name,
                })
            _entry(archive, "expenses.csv", _csv(EXPENSE_FIELDS, expense_rows))
        digest = file_digest(temporary)
        path = directory / f"office-{start}-to-{end}-{digest[:12]}.zip"
        if not path.is_file() or file_digest(path) != digest:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(path), "sha256": digest, "invoices": len(invoices), "payments": len(payments),
            "expenses": len(expenses), "from_date": start, "to_date": end}
