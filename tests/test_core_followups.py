from datetime import datetime, timedelta, timezone

import pytest

from test_core_ledger import CUSTOMER, OWNER, ledger
from chalkline.core import queries, service
from chalkline.core.followups import appointment_reminder, payment_chaser, quote_followup
from chalkline.core.schema import connect


NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def invoice_job():
    service.configure({"payment_terms_days": 0}, actor="cli")
    job = service.capture_enquiry(CUSTOMER, text="Repair tap")["job_id"]
    service.complete_job(job, actor=OWNER, items=[{"description": "Repair", "unit_price": "20"}])
    invoice = service.draft_invoice(job, actor=OWNER, now=NOW)
    ticket = service.request_issue("invoice", invoice["id"], actor=OWNER, now=NOW)
    service.approve(ticket["code"], actor=OWNER, now=NOW)
    service.claim_outbox(ticket["outbox_id"], now=NOW)
    service.finish_outbox(ticket["outbox_id"], "sent", now=NOW)
    return job, invoice


def test_chaser_stages_and_payment_hold_void_cancel_pending(ledger):
    job, invoice = invoice_job()
    assert queries.due_actions(NOW + timedelta(days=6)) == []
    for days in (7, 14, 30):
        now = NOW + timedelta(days=days)
        [action] = queries.due_actions(now)
        assert action["stage_key"] == f"payment_chaser:{invoice['id']}:day{days}"
        row = service.enqueue_message(**action, now=now)
        assert queries.due_actions(now) == []
        if days == 7:
            service.set_invoice_status(invoice["id"], "on_hold", actor=OWNER)
            assert queries.get_outbox(row["id"])["status"] == "cancelled"
            assert queries.due_actions(now) == []
            service.set_invoice_status(invoice["id"], "sent", actor=OWNER)
        elif days == 14:
            payment = service.request_payment(invoice["id"], "1", actor=OWNER, now=now)
            service.approve(payment["code"], actor=OWNER, now=now)
            assert queries.get_outbox(row["id"])["status"] == "cancelled"
        else:
            payment = service.request_payment(invoice["id"], "20", actor=OWNER, now=now)
            service.approve(payment["code"], actor=OWNER, now=now)
            assert queries.get_outbox(row["id"])["status"] == "cancelled"
    assert all(action["kind"] != "payment_chaser" for action in queries.due_actions(NOW + timedelta(days=45)))
    assert queries.job_detail(job)["job"]["state"] == "paid"


def test_quote_followup_and_rescheduled_reminder_use_exact_record(ledger):
    job = service.capture_enquiry(CUSTOMER, text="Repair tap")["job_id"]
    quote = service.draft_quote(job, [{"description": "Repair", "unit_price": "20"}], actor=OWNER, now=NOW)
    approval = service.request_issue("quote", quote["id"], actor=OWNER, now=NOW)
    service.approve(approval["code"], actor=OWNER, now=NOW)
    service.claim_outbox(approval["outbox_id"], now=NOW)
    service.finish_outbox(approval["outbox_id"], "sent", now=NOW)
    now = NOW + timedelta(days=3)
    [action] = queries.due_actions(now)
    row = service.enqueue_message(**action, now=now)
    service.record_quote_decision(quote["id"], "accepted", actor=OWNER, now=now)
    assert queries.get_outbox(row["id"])["status"] == "cancelled"
    appointment = service.schedule(job, now + timedelta(hours=24), now + timedelta(hours=25), actor=OWNER, now=now)
    template = service.propose_template("appointment_reminder", "Your appointment is {appointment_start}.", actor=OWNER, now=now)
    service.approve(template["code"], actor=OWNER, now=now)
    [action] = queries.due_actions(now)
    reminder = service.enqueue_message(**action, now=now)
    assert reminder["stage_key"] == f"appointment_reminder:{appointment['id']}:24h"
    service.schedule(job, now + timedelta(hours=48), now + timedelta(hours=49), actor=OWNER, now=now)
    assert service.claim_outbox(reminder["id"], now=now) is None
    assert queries.due_actions(now) == []


def test_latest_stage_only_and_digest_are_state_driven(ledger):
    job, invoice = invoice_job()
    now = NOW + timedelta(days=30)
    actions = queries.due_actions(now)
    assert len(actions) == 1 and actions[0]["stage_key"].endswith("day30")
    digest = queries.digest(now)
    assert digest["overdue_invoices"][0]["number"] == invoice["number"]
    assert digest["pending_approvals"] == [] and digest["appointments"] == []
    service.set_invoice_status(invoice["id"], "void", actor=OWNER)
    assert queries.due_actions(now) == []
    assert queries.digest(now)["overdue_invoices"] == []


def test_test_clock_applies_only_to_dev_profile(ledger, monkeypatch):
    monkeypatch.setenv("CHALKLINE_TEST_NOW", "2026-11-17T12:00:00+00:00")
    assert queries.clock_now() == NOW + timedelta(days=7)
    monkeypatch.setenv("HERMES_HOME", str(ledger.parent / "chalkline-ui"))
    assert queries.clock_now() == NOW + timedelta(days=7)
    monkeypatch.setenv("HERMES_HOME", str(ledger.parent / "production"))
    with pytest.raises(ValueError, match="only in chalkline-dev"):
        queries.clock_now()
