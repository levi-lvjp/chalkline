from pathlib import Path

import pytest

from test_core_approvals import NOW, job_with_customer
from test_core_followups import invoice_job
from test_core_ledger import CUSTOMER, OWNER, ledger
from chalkline.core import queries, service


def test_repeat_completion_cannot_reopen_issued_invoice(ledger):
    job, invoice = invoice_job()
    with pytest.raises(ValueError, match="uninvoiced"):
        service.complete_job(job, actor=OWNER)
    assert queries.job_detail(job)["job"]["state"] == "invoiced"
    ticket = service.request_payment(invoice["id"], "21", actor=OWNER, now=NOW)
    service.approve(ticket["code"], actor=OWNER, now=NOW)
    assert queries.job_detail(job)["job"]["state"] == "paid"


def test_unassigned_receipt_can_later_bill_exact_job(ledger):
    image = ledger / "receipt.png"
    image.write_bytes(b"fictional receipt")
    fields = {"vendor": "Fictional Parts", "date": "2026-11-10", "total": "12",
              "currency": "CAD", "confidence": "high"}
    receipt = service.propose_receipt(image, fields, actor=OWNER)
    ticket = service.request_expense_confirmation(receipt["id"], actor=OWNER, now=NOW)
    service.approve(ticket["code"], actor=OWNER, now=NOW)
    job = job_with_customer()
    assigned = service.propose_receipt(image, fields, actor=OWNER, job=job)
    assert assigned["duplicate"] and assigned["id"] == receipt["id"] and assigned["job_id"] == job
    quote = service.draft_quote(job, [{"description": "Parts", "kind": "material", "expense_id": receipt["id"]}],
                                actor=OWNER, now=NOW)
    assert quote["subtotal_minor"] == 1440
    other = service.capture_owner(actor=OWNER, name="Other Fictional Customer", text="Other job")["job_id"]
    with pytest.raises(ValueError, match="already attached"):
        service.assign_expense(receipt["id"], other, actor=OWNER)


def test_lid_capture_is_reusable_and_bad_display_name_is_safe(ledger):
    first = service.capture_unresolved_enquiry("123456789@lid", text="Leaking tap", message_id="lid-1", name="   ")
    second = service.capture_unresolved_enquiry("123456789@lid", text="More details", message_id="lid-2")
    assert first["job_id"] == second["job_id"]
    detail = queries.job_detail(first["job_id"])
    assert detail["customer"]["phone"] is None and len(detail["sources"]) == 2
    customer = service.capture_enquiry(CUSTOMER, text="Leaking tap", name="  " + "x" * 500)
    assert len(queries.job_detail(customer["job_id"])["customer"]["name"]) == 200


def test_nested_transport_tokens_in_customer_name_do_not_break_standing_template(ledger):
    job = service.capture_enquiry(CUSTOMER, text="Leaking tap", name="MEDMEDIA:IA:")["job_id"]
    ticket = service.propose_template("enquiry_ack", "Thanks {customer_name}.", actor=OWNER, now=NOW)
    service.approve(ticket["code"], actor=OWNER, now=NOW)
    row = service.enqueue_message("enquiry_ack", CUSTOMER, "", "ack", standing_template="enquiry_ack",
                                   job_id=job, now=NOW)
    assert "MEDIA:" not in row["body"]
    assert service.claim_outbox(row["id"], now=NOW) is not None


def test_unknown_and_cancelled_sends_create_one_owner_notice(ledger):
    row = service.enqueue_message("other", CUSTOMER, "A reviewed message", "message", now=NOW)
    approval = service.approve(row["code"], actor=OWNER, now=NOW)
    assert approval["summary"] and approval["recipient"] == CUSTOMER
    service.claim_outbox(row["id"], now=NOW)
    service.finish_outbox(row["id"], "unknown", detail="Transport timeout", now=NOW)
    service.finish_outbox(row["id"], "unknown", now=NOW)
    [notice] = queries.delivery_notices(row["id"])
    assert notice["recipient"] == OWNER and "unknown" in notice["body"]
    assert "Transport timeout" in notice["body"]


def test_completion_and_booking_tools_only_propose(ledger):
    from datetime import timedelta
    from test_core_host import Context

    ctx = Context()
    ctx.observe_turn("whatsapp", OWNER)
    job = job_with_customer()
    number = queries.job_detail(job)["job"]["number"]
    proposal = ctx.call("office_complete", {"job": number, "items": [{"description": "Repair", "unit_price": "20"}]})
    assert proposal["status"] == "awaiting_approval"
    assert queries.job_detail(job)["job"]["state"] == "enquiry"
    service.approve(proposal["code"], actor=OWNER)
    assert queries.job_detail(job)["job"]["state"] == "done"

    other = service.capture_owner(actor=OWNER, name="Other Fictional Customer", phone="+12025550108", text="Repair tap")["job_id"]
    quote = service.draft_quote(other, [{"description": "Repair", "unit_price": "20"}], actor=OWNER, now=NOW)
    issue = service.request_issue("quote", quote["id"], actor=OWNER, now=NOW)
    service.approve(issue["code"], actor=OWNER, now=NOW)
    service.record_quote_decision(quote["id"], "accepted", actor=OWNER, now=NOW)
    booking = service.request_schedule(other, NOW + timedelta(days=1), NOW + timedelta(days=1,hours=1), actor=OWNER, now=NOW)
    assert queries.job_detail(other)["appointments"] == []
    service.approve(booking["code"], actor=OWNER, now=NOW)
    assert len(queries.job_detail(other)["appointments"]) == 1


def test_authenticated_dashboard_requests_keep_actor_and_job_timeline(ledger):
    job, invoice = invoice_job()
    proposal = service.request_payment(invoice["id"], "21", actor="dashboard", now=NOW)
    service.approve(proposal["code"], actor="dashboard", now=NOW)
    detail = queries.job_detail(job)
    assert detail["payments"][0]["recorded_by"] == "dashboard"
    timeline = queries.job_events(job)
    assert any(row["entity"] == "payment" and row["actor"] == "dashboard" for row in timeline)
    assert any(row["entity"] == "approval" and row["actor"] == "dashboard" for row in timeline)
    assert [row["id"] for row in timeline] == sorted(row["id"] for row in timeline)
    assert service.export_books("2026-11-10", "2026-11-10")["payments"] == 1
