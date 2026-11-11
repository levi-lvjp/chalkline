from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from test_core_ledger import CUSTOMER, OWNER, ledger
from chalkline.core import queries, service
from chalkline.core.schema import connect


NOW = datetime(2026, 11, 10, 18, tzinfo=timezone.utc)


def job_with_customer():
    return service.capture_enquiry(CUSTOMER, text="Replace leaking tap.")["job_id"]


def draft_quote(job, amount="10"):
    return service.draft_quote(job, [{"description": "Repair", "unit_price": amount}], actor=OWNER, now=NOW)


def test_wrong_sender_stale_revision_and_replay_cannot_send(ledger):
    job = job_with_customer()
    first = draft_quote(job)
    stale = service.request_issue("quote", first["id"], actor=OWNER, now=NOW)
    with pytest.raises(PermissionError):
        service.approve(stale["code"], actor=CUSTOMER, now=NOW)
    assert service.claim_outbox(stale["outbox_id"], now=NOW) is None
    second = draft_quote(job, "20")
    active = service.request_issue("quote", second["id"], actor=OWNER, now=NOW)
    assert first["number"] == second["number"] and second["revision"] == 2
    assert [row["code"] for row in queries.pending_approvals(NOW)] == [active["code"]]
    with pytest.raises(ValueError, match="invalidated"):
        service.approve(stale["code"], actor=OWNER, now=NOW)
    service.approve(active["code"], actor=OWNER, now=NOW)
    with pytest.raises(ValueError, match="already been used"):
        service.approve(active["code"], actor=OWNER, now=NOW)
    claimed = service.claim_outbox(active["outbox_id"], now=NOW)
    assert claimed["recipient"] == CUSTOMER and "revision 2" in claimed["body"]
    assert service.claim_outbox(stale["outbox_id"], now=NOW) is None


def test_expiry_and_payload_change_require_new_code(ledger):
    job = job_with_customer()
    quote = draft_quote(job)
    approval = service.request_issue("quote", quote["id"], actor=OWNER, now=NOW)
    tomorrow = NOW + timedelta(days=1)
    with pytest.raises(ValueError, match="expired"):
        service.approve(approval["code"], actor=OWNER, now=tomorrow)
    renewal = service.request_issue("quote", quote["id"], actor=OWNER, now=tomorrow)
    assert approval["code"] != renewal["code"] and approval["outbox_id"] == renewal["outbox_id"]
    with connect(write=True) as db:
        db.execute("UPDATE outbox SET body='Changed' WHERE id=?", (renewal["outbox_id"],))
    with pytest.raises(ValueError, match="payload has changed"):
        service.approve(renewal["code"], actor=OWNER, now=tomorrow)
    replacement = service.request_issue("quote", quote["id"], actor=OWNER, now=tomorrow)
    assert replacement["code"] != renewal["code"]
    service.reject(replacement["code"], actor=OWNER, now=tomorrow)
    assert queries.get_outbox(replacement["outbox_id"])["status"] == "cancelled"


def test_owner_route_change_invalidates_pending_and_approved_payloads(ledger):
    job = job_with_customer()
    approval = service.request_issue("quote", draft_quote(job)["id"], actor=OWNER, now=NOW)
    with pytest.raises(PermissionError):
        service.approve(approval["code"], actor="telegram:111:111", now=NOW)
    service.configure({"telegram_owner_user_id": "111", "telegram_owner_chat_id": "111"}, actor="cli")
    with pytest.raises(ValueError, match="owner route has changed"):
        service.approve(approval["code"], actor=OWNER, now=NOW)
    fresh = service.request_issue("quote", draft_quote(job, "20")["id"], actor=OWNER, now=NOW)
    service.approve(fresh["code"], actor="telegram:111:111", now=NOW)
    service.configure({"telegram_owner_user_id": "222", "telegram_owner_chat_id": "222"}, actor="cli")
    assert service.claim_outbox(fresh["outbox_id"], now=NOW) is None


def test_standing_ack_cooldown_and_template_edit(ledger):
    job = job_with_customer()

    def enqueue(stage, now=NOW):
        return service.enqueue_message("enquiry_ack", CUSTOMER, "", stage, standing_template="enquiry_ack",
                                       job_id=job, now=now)

    assert enqueue("before") is None
    template = service.propose_template("enquiry_ack", "Thanks, {customer_name}.", actor=OWNER, now=NOW)
    service.approve(template["code"], actor="cli", now=NOW)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: enqueue(f"ack:{i}"), range(6)))
    [queued] = [row for row in results if row]
    service.claim_outbox(queued["id"], now=NOW)
    service.finish_outbox(queued["id"], "unknown", now=NOW)
    assert enqueue("suppressed", NOW + timedelta(hours=11, minutes=59)) is None
    next_ack = enqueue("next", NOW + timedelta(hours=12))
    edited = service.propose_template("enquiry_ack", "New text.", actor="cli", now=NOW)
    assert service.claim_outbox(next_ack["id"], now=NOW + timedelta(hours=12)) is None
    service.approve(edited["code"], actor=OWNER, now=NOW)
    assert enqueue("edited", NOW + timedelta(hours=12))["body"] == "New text."


def test_invoice_issue_payment_and_interrupted_delivery(ledger):
    job = job_with_customer()
    service.complete_job(job, actor=OWNER, items=[{"description": "Repair", "unit_price": 20}])
    invoice = service.draft_invoice(job, actor=OWNER, now=NOW)
    issue = service.request_issue("invoice", invoice["id"], actor=OWNER, now=NOW)
    service.approve(issue["code"], actor="cli", now=NOW)
    assert queries.money_owed()[0]["balance_minor"] == 2100
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(lambda _: service.claim_outbox(issue["outbox_id"], now=NOW), range(4)))
    assert sum(claim is not None for claim in claims) == 1
    assert queries.ready_outbox() == []
    assert service.recover_interrupted_sends(NOW + timedelta(minutes=5)) == 1
    assert queries.get_outbox(issue["outbox_id"])["status"] == "unknown"
    assert queries.job_detail(job)["invoices"][0]["delivery_status"] == "unknown"
    assert service.claim_outbox(issue["outbox_id"], now=NOW + timedelta(days=7)) is None
    assert queries.job_detail(job)["payments"] == []
    with pytest.raises(PermissionError):
        service.request_payment(invoice["id"], 21, actor=CUSTOMER, now=NOW)
    payment = service.request_payment(invoice["id"], 21, actor=OWNER, request_key="owner-msg-2", now=NOW)
    assert queries.job_detail(job)["payments"] == []
    assert service.approve(payment["code"], actor=OWNER, now=NOW)["balance_minor"] == 0
    detail = queries.job_detail(job)
    assert detail["job"]["state"] == detail["invoices"][0]["status"] == "paid"
    with pytest.raises(ValueError, match="already recorded"):
        service.request_payment(invoice["id"], 21, actor=OWNER, request_key="owner-msg-2", now=NOW)


def test_corrected_receipt_approval_requires_exact_fields(ledger):
    image = ledger / "receipt.png"
    image.write_bytes(b"fictional receipt")
    fields = {"vendor": "Fictional Parts", "date": "2026-11-10", "total": 12, "currency": "CAD", "confidence": "low"}
    receipt = service.propose_receipt(image, fields, actor=OWNER)
    with pytest.raises(ValueError, match="confidence"):
        service.request_expense_confirmation(receipt["id"], actor=OWNER, now=NOW)
    service.correct_expense(receipt["id"], actor="cli", vendor="Fictional Parts", payment_date="2026-11-10", amount="21")
    old = service.request_expense_confirmation(receipt["id"], actor=OWNER, now=NOW)
    service.correct_expense(receipt["id"], actor="cli", vendor="Fictional Parts", payment_date="2026-11-10", amount="22")
    with pytest.raises(ValueError, match="invalidated"):
        service.approve(old["code"], actor=OWNER, now=NOW)
    new = service.request_expense_confirmation(receipt["id"], actor=OWNER, now=NOW)
    service.approve(new["code"], actor=OWNER, now=NOW)
    assert queries.expenses()[0]["amount_minor"] == 2200
    assert queries.expenses()[0]["status"] == "confirmed"
