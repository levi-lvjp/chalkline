from datetime import timedelta

import pytest

from test_core_ledger import OWNER, CUSTOMER, ledger
from test_core_followups import NOW, invoice_job
from chalkline.core import queries, service
from chalkline.core.schema import connect


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "unknown"])
def test_chasers_require_confirmed_original_delivery(ledger, outcome):
    job, invoice = invoice_job()
    with connect(write=True) as db:
        db.execute("UPDATE outbox SET status=? WHERE kind='invoice' AND related_id=?", (outcome, invoice["id"]))
    assert not any(row["job_id"] == job for row in queries.due_actions(NOW + timedelta(days=7)))


def test_historical_paid_job_cannot_receive_standing_review(ledger):
    job, invoice = invoice_job()
    payment = service.request_payment(invoice["id"], "21", actor=OWNER, now=NOW)
    service.approve(payment["code"], actor=OWNER, now=NOW)
    template = service.propose_template("review_request", "Thanks for your custom.", actor=OWNER, now=NOW)
    service.approve(template["code"], actor=OWNER, now=NOW)
    assert not any(row["kind"] == "review_request" and row["job_id"] == job
                   for row in queries.due_actions(NOW + timedelta(days=7)))
    assert service.enqueue_message("review_request", CUSTOMER, "", "historical-review",
        job_id=job, standing_template="review_request", now=NOW + timedelta(days=7)) is None


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "unknown"])
def test_quote_followups_require_confirmed_original_delivery(ledger, outcome):
    job = service.capture_enquiry(CUSTOMER, text="Repair")["job_id"]
    quote = service.draft_quote(job, [{"description": "Repair", "unit_price": "20"}], actor=OWNER, now=NOW)
    ticket = service.request_issue("quote", quote["id"], actor=OWNER, now=NOW)
    service.approve(ticket["code"], actor=OWNER, now=NOW)
    with connect(write=True) as db:
        db.execute("UPDATE outbox SET status=? WHERE id=?", (outcome, ticket["outbox_id"]))
    assert not any(row["job_id"] == job for row in queries.due_actions(NOW + timedelta(days=3)))
