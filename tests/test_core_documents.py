import csv
import io
from pathlib import Path
from zipfile import ZipFile

import pytest

from test_core_approvals import NOW, draft_quote, job_with_customer
from test_core_followups import invoice_job
from test_core_ledger import OWNER, ledger
from chalkline.core import queries, service
from chalkline.core.export import export_books


def test_quote_pdf_is_deterministic_and_exact_bytes_are_approved(ledger):
    quote = draft_quote(job_with_customer())
    ticket = service.request_issue("quote", quote["id"], actor=OWNER, now=NOW)
    path = Path(ticket["attachment_path"])
    original = path.read_bytes()
    assert path.suffix == ".pdf" and original.startswith(b"%PDF-")
    assert service.request_issue("quote", quote["id"], actor=OWNER, now=NOW)["code"] == ticket["code"]
    assert path.read_bytes() == original
    assert queries.get_outbox(ticket["outbox_id"])["attachment_path"] == str(path.resolve())
    service.approve(ticket["code"], actor=OWNER, now=NOW)
    path.write_bytes(original + b"changed")
    assert service.claim_outbox(ticket["outbox_id"], now=NOW) is None
    assert queries.get_outbox(ticket["outbox_id"])["status"] == "cancelled"
    assert queries.delivery_notices(ticket["outbox_id"])[0]["recipient"] == OWNER


def test_accountant_export_contains_invoice_payment_and_one_confirmed_receipt(ledger):
    job, invoice = invoice_job()
    image = ledger / "receipt.png"
    image.write_bytes(b"fictional receipt bytes")
    fields = {"vendor": "=Fictional Parts", "date": "2026-11-10", "total": "12.34",
              "currency": "CAD", "confidence": "high"}
    expense = service.propose_receipt(image, fields, actor=OWNER, job=job)
    ticket = service.request_expense_confirmation(expense["id"], actor=OWNER, now=NOW)
    service.approve(ticket["code"], actor=OWNER, now=NOW)
    payment = service.request_payment(invoice["id"], "21.00", actor=OWNER, now=NOW, reference="TEST-PAID")
    service.approve(payment["code"], actor=OWNER, now=NOW)
    first = export_books("2026-11-10", "2026-11-10")
    second = export_books("2026-11-10", "2026-11-10")
    assert first == second
    assert (first["invoices"], first["payments"], first["expenses"]) == (1, 1, 1)
    data = Path(first["path"]).read_bytes()
    with ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["income.csv", "receipts/", "receipts/receipt-0001.png", "expenses.csv"]
        assert archive.read("receipts/receipt-0001.png") == image.read_bytes()
        expenses = list(csv.DictReader(io.StringIO(archive.read("expenses.csv").decode("utf-8-sig"))))
        assert expenses[0]["vendor"] == "'=Fictional Parts"
        assert expenses[0]["total"] == "12.34" and expenses[0]["job"] == queries.job_detail(job)["job"]["number"]
        income = list(csv.DictReader(io.StringIO(archive.read("income.csv").decode("utf-8-sig"))))
        assert {row["type"] for row in income} == {"invoice", "payment"}
        assert all(row["total"] == "21.00" for row in income)
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
    outside = export_books("2026-11-09", "2026-11-09")
    assert (outside["invoices"], outside["payments"], outside["expenses"]) == (0, 0, 0)
    with pytest.raises(ValueError, match="start date"):
        export_books("2026-11-11", "2026-11-10")


def test_bundled_font_supports_extended_latin(ledger):
    job = service.capture_owner(actor=OWNER, name=chr(321) + "ukasz", text="Repair")["job_id"]
    quote = service.draft_quote(job, [{"description": "Repair", "unit_price": "12"}], actor=OWNER, now=NOW)
    ticket = service.request_issue("quote", quote["id"], actor=OWNER, now=NOW)
    assert Path(ticket["attachment_path"]).read_bytes().startswith(b"%PDF-")
