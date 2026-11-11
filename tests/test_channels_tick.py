from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from chalkline.channels import outbound, tick
from chalkline.core import queries, service


NOW = datetime(2026, 11, 10, 18, tzinfo=timezone.utc)
OWNER = "+12025550101"
CUSTOMER = "+12025550109"
LABOUR = [{"description": "Repair labour", "kind": "labour", "qty": "1"}]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "chalkline-dev"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    service.configure({
        "business_name": "Mint Tap Repairs", "owner_phones": [OWNER],
        "timezone": "Europe/London", "digest_time": "18:00",
        "hourly_rate": "55.00", "payment_terms_days": 0,
    }, actor="cli")
    monkeypatch.setattr(outbound, "_send_whatsapp", Mock(return_value=("sent", "")))
    return home


def _approved_template():
    proposal = service.propose_template(
        "appointment_reminder", "Reminder for {job_number}: {appointment_start}.", actor="cli", now=NOW,
    )
    service.approve(proposal["code"], actor="cli", now=NOW)


def _issue(kind, record, now):
    issue = service.request_issue(kind, record["id"], actor="cli", now=now)
    service.approve(issue["code"], actor="cli", now=now)
    assert outbound.deliver_outbox(issue["outbox_id"], now=now) == "sent"
    for row in queries.ready_outbox():
        if row["kind"] == "owner_notice":
            outbound.deliver_outbox(row["id"], now=now)


def _scheduled_job(start, *, as_of=NOW):
    job = service.capture_enquiry(CUSTOMER, text="Leaking tap.", name="Sarah")["job_id"]
    quote = service.draft_quote(job, LABOUR, actor="cli", now=as_of)
    _issue("quote", quote, as_of)
    service.record_quote_decision(quote["id"], "accepted", actor="cli", now=as_of)
    appointment = service.schedule(job, start, start + timedelta(hours=1), actor="cli", now=as_of)
    outbound._send_whatsapp.reset_mock()
    return job, appointment


def _suppress_daily_digest():
    service.configure({"digest_time": "23:59"}, actor="cli")


def test_tick_sends_daily_digest_and_standing_reminder_once(ledger):
    _approved_template()
    job, _ = _scheduled_job(NOW + timedelta(hours=20), as_of=NOW - timedelta(minutes=30))
    pending = service.enqueue_message("other", CUSTOMER, "Pending approval.", "test:pending", now=NOW)

    first = tick.run_tick(now=NOW - timedelta(minutes=15))
    assert first["sent"] == 1 and first["errors"] == 0
    digest = tick.run_tick(now=NOW)
    assert digest["sent"] >= 1 and digest["errors"] == 0
    assert tick.run_tick(now=NOW + timedelta(minutes=15))["sent"] == 0
    assert tick.run_tick(now=NOW + timedelta(days=1))["sent"] == 1

    calls = outbound._send_whatsapp.call_args_list
    assert len([call for call in calls if call.args[0] == CUSTOMER]) == 1
    digests = [call.args[1] for call in calls if call.args[1].startswith("Chalkline daily digest")]
    assert len(digests) == 2
    job_number = queries.job_detail(job)["job"]["number"]
    assert all(label in digests[0] for label in [pending["code"], job_number, "Overdue invoices", "Unconfirmed receipts"])


def test_restart_after_bridge_send_before_result_recorded_never_resends(ledger, monkeypatch):
    _suppress_daily_digest()
    row = service.enqueue_owner_notice(OWNER, "Fictional notice.", "test:crash", now=NOW)
    original_finish = service.finish_outbox
    monkeypatch.setattr(service, "finish_outbox", Mock(side_effect=SystemExit("simulated restart")))
    with pytest.raises(SystemExit):
        tick.run_tick(now=NOW)
    assert queries.get_outbox(row["id"])["status"] == "sending"
    monkeypatch.setattr(service, "finish_outbox", original_finish)
    recovered = tick.run_tick(now=NOW + timedelta(minutes=6))
    assert recovered["sent"] == 0 and recovered["unknown"] == 1
    assert queries.get_outbox(row["id"])["status"] == "unknown"
    outbound._send_whatsapp.assert_called_once()


def test_tick_reminders_follow_reschedule_and_cancel(ledger):
    _suppress_daily_digest()
    _approved_template()
    job, original = _scheduled_job(NOW + timedelta(hours=23))
    due = next(action for action in queries.due_actions(NOW) if action["related_id"] == original["id"])
    stale = service.enqueue_message(**due, now=NOW)
    new_start = NOW + timedelta(days=2)
    replacement = service.schedule(job, new_start, new_start + timedelta(hours=1), actor="cli", now=NOW)
    assert replacement["id"] != original["id"]
    assert queries.get_outbox(stale["id"])["status"] == "cancelled"
    tick.run_tick(now=NOW)
    assert not any(call.args[0] == CUSTOMER for call in outbound._send_whatsapp.call_args_list)
    assert tick.run_tick(now=new_start - timedelta(hours=24))["sent"] == 1
    customer_calls = [call for call in outbound._send_whatsapp.call_args_list if call.args[0] == CUSTOMER]
    assert "2026-11-12 18:00" in customer_calls[0].args[1]
    service.cancel_job(job, actor="cli")
    tick.run_tick(now=new_start - timedelta(minutes=45))
    assert len([call for call in outbound._send_whatsapp.call_args_list if call.args[0] == CUSTOMER]) == 1


def test_tick_queues_chaser_then_stops_after_payment(ledger):
    _suppress_daily_digest()
    issued_at = NOW - timedelta(days=7)
    job = service.capture_enquiry(CUSTOMER, text="Repair already completed.", name="Sarah")["job_id"]
    service.complete_job(job, actor="cli", items=LABOUR)
    invoice = service.draft_invoice(job, actor="cli", now=issued_at)
    _issue("invoice", invoice, issued_at)
    outbound._send_whatsapp.reset_mock()

    first = tick.run_tick(now=NOW)
    assert first["queued"] == 1 and first["sent"] == 0 and first["errors"] == 0
    pending = queries.pending_approvals(NOW)
    assert [row["kind"] for row in pending] == ["payment_chaser"]
    assert tick.run_tick(now=NOW + timedelta(minutes=15))["queued"] == 0
    payment = service.request_payment(invoice["id"], "55.00", actor="cli", now=NOW)
    service.approve(payment["code"], actor="cli", now=NOW)
    assert not queries.pending_approvals(NOW)
    assert tick.run_tick(now=NOW + timedelta(days=7))["queued"] == 0
    assert tick.run_tick(now=NOW + timedelta(days=23))["queued"] == 0
    assert not any(call.args[0] == CUSTOMER for call in outbound._send_whatsapp.call_args_list)


def test_tick_only_queues_followup_for_the_sent_quote_revision(ledger):
    _suppress_daily_digest()
    issued_at = NOW - timedelta(days=4)
    job = service.capture_enquiry(CUSTOMER, text="Needs a quote.", name="Sarah")["job_id"]
    quote = service.draft_quote(job, LABOUR, actor="cli", now=issued_at)
    assert tick.run_tick(now=NOW)["queued"] == 0
    _issue("quote", quote, issued_at)
    outbound._send_whatsapp.reset_mock()
    assert tick.run_tick(now=NOW)["queued"] == 1
    assert [row["kind"] for row in queries.pending_approvals(NOW)] == ["quote_followup"]
    service.draft_quote(job, LABOUR, actor="cli", now=NOW)
    assert queries.pending_approvals(NOW) == []
    assert tick.run_tick(now=NOW + timedelta(days=4))["queued"] == 0
    assert not any(call.args[0] == CUSTOMER for call in outbound._send_whatsapp.call_args_list)


def test_hermes_no_agent_job_runs_real_channels_against_profile_db(ledger):
    from cron.jobs import get_job, list_jobs
    from cron.scheduler import run_job
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    now = datetime.now(timezone.utc)
    abandoned = service.enqueue_owner_notice(OWNER, "Abandoned notice.", "test:abandoned", now=now)
    assert service.claim_outbox(abandoned["id"], now=now - timedelta(minutes=10))
    for offset in (0, 1):
        day = (now.astimezone(ZoneInfo("Europe/London")) + timedelta(days=offset)).date().isoformat()
        notice = service.enqueue_owner_notice(OWNER, "Already sent digest.", f"daily-digest:{day}", now=now)
        service.claim_outbox(notice["id"], now=now)
        service.finish_outbox(notice["id"], "sent", now=now)

    ctx = PluginContext(PluginManifest(name="chalkline"), get_plugin_manager())
    job_id = tick.ensure_tick(ctx)["job_id"]
    assert tick.ensure_tick(ctx)["job_id"] == job_id
    assert len(list_jobs(include_disabled=True)) == 1
    job = get_job(job_id)
    assert job["no_agent"] is True
    assert job["schedule_display"] == "*/15 * * * *"
    assert (ledger / "scripts" / Path(job["script"])).is_file()
    success, output, final, error = run_job(job)
    assert success and final == "[SILENT]", (output, error)
    assert queries.get_outbox(abandoned["id"])["status"] == "unknown"
