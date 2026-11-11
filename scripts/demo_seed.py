"""Seed fictional office records. Run with the Hermes Python and plugin dependencies."""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
REPO = Path(__file__).resolve().parents[1]
DEFAULT_CLOCK = "2026-11-11T10:00:00+00:00"
NAMES = ["Amara Okafor", "Hiro Tanaka", "Lucía Torres", "Arjun Mehta",
         "Mina Park", "Noor Haddad", "Sofia Rossi", "Mateo Silva",
         "Aisha Khan", "Emil Novak", "Lina Chen", "Kwame Mensah"]
STATES = ["enquiry", "quoted", "quoted", "accepted", "scheduled", "scheduled",
          "done", "invoiced", "invoiced", "paid", "declined", "cancelled"]


def seed(*, reset=False, profile_name="chalkline-ui", force=False):
    if profile_name not in ("chalkline-dev", "chalkline-ui"):
        raise ValueError("Choose a dedicated workspace demo profile.")
    profile = WORKSPACE / ".runtime" / "profiles" / profile_name
    if not (profile / "config.yaml").is_file():
        raise ValueError("Create the workspace-local chalkline-dev profile first.")
    data = profile / "plugin-data" / "chalkline"
    for path in [WORKSPACE / ".runtime", profile.parent, profile, data.parent, data]:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("The demo profile must use ordinary workspace directories.")
    if data.resolve().parent != (profile / "plugin-data").resolve():
        raise ValueError("Unexpected plugin data location.")
    os.environ["HERMES_HOME"] = str(profile)
    from gateway.status import get_running_pid_identity_strict
    from chalkline.core import queries, service

    if get_running_pid_identity_strict(profile / "gateway.pid"):
        raise ValueError("Stop chalkline-dev before seeding.")
    marker = data / "demo-seed.json"
    if reset and data.exists():
        if not marker.exists() and not force:
            raise ValueError("Refusing to reset an unmarked ledger. Explicit --force is required.")
        shutil.rmtree(data)
    elif marker.exists():
        return json.loads(marker.read_text(encoding="utf-8"))
    elif data.exists() and any(data.iterdir()):
        raise ValueError("Plugin data already exists. Use --reset to replace this plugin's demo data.")

    now = datetime.fromisoformat(os.environ.get("CHALKLINE_TEST_NOW") or DEFAULT_CLOCK)
    if now.utcoffset() is None:
        raise ValueError("The demo clock needs an explicit UTC offset.")

    def clock(instant):
        os.environ["CHALKLINE_TEST_NOW"] = instant.isoformat()

    def approve(ticket):
        return service.approve(ticket["code"], actor="cli")

    def issue(kind, record):
        ticket = service.request_issue(kind, record["id"], actor="cli")
        approve(ticket)
        row = service.claim_outbox(ticket["outbox_id"])
        if row is None:
            raise ValueError("Could not claim the fictional delivery.")
        service.finish_outbox(row["id"], "sent", detail="Fictional demo delivery. No transport called.")

    clock(now)
    service.configure({
        "business_name": "Northstar Home Repairs — Fictional Demo",
        "owner_phones": ["+12025550100"], "currency": "USD", "locale": "en-US",
        "timezone": "UTC", "hourly_rate": "75.00", "payment_terms_days": 0,
        "payment_text": "Fictional demo. No payment requested.", "digest_time": "17:00",
    }, actor="cli")
    fixture = REPO / "fixtures" / "receipt.png"
    jobs = []
    for index, (name, state) in enumerate(zip(NAMES, STATES)):
        instant = now - timedelta(days=9 if index == 7 else 1)
        clock(instant)
        job = service.capture_enquiry(
            f"+1202555{index + 101:04d}", name=name, text=f"Fictional repair enquiry from {name}.",
            message_id=f"demo-enquiry-{index}",
            media=[{"kind": "photo", "path": fixture}] if index in (0, 4, 6) else (),
        )
        jobs.append(job["job_id"])
        service.capture_owner(
            actor="cli", job=job["job_id"],
            text="Voice-note transcript (fictional): inspect the tap, replace the seal, and test for leaks.",
            message_id=f"demo-transcript-{index}",
        )
        if state == "enquiry":
            continue
        if state == "cancelled":
            service.cancel_job(job["job_id"], actor="cli")
            continue
        lines = [{"description": "Repair labour", "kind": "labour", "qty": "2"},
                 {"description": "Seal kit", "kind": "other", "qty": "1", "unit_price": "18.00"}]
        quote = service.draft_quote(job["job_id"], lines, actor="cli")
        issue("quote", quote)
        if index == 2:
            revised = service.draft_quote(job["job_id"], lines + [
                {"description": "Additional fittings", "kind": "other", "unit_price": "12.00"},
            ], actor="cli")
            clock(now)
            service.request_issue("quote", revised["id"], actor="cli")
        if state == "quoted":
            continue
        service.record_quote_decision(quote["id"], "declined" if state == "declined" else "accepted", actor="cli")
        if state in ("accepted", "declined"):
            continue
        if state == "scheduled":
            clock(now)
            start = (now + timedelta(days=index - 3)).replace(hour=9, minute=0, second=0, microsecond=0)
            service.schedule(job["job_id"], start, start + timedelta(hours=2), actor="cli")
            continue
        if state == "done":
            source = service.capture_owner(
                actor="cli", job=job["job_id"], text="Fictional transcript: customer agreed to a new valve for USD 25.",
            )["source_ids"][0]
            extra = service.propose_extra(job["job_id"], "Agreed replacement valve", "25.00", actor="cli")
            approve(service.request_extra_decision(extra["id"], source, actor="cli"))
            service.propose_extra(job["job_id"], "Optional hose replacement", "40.00", actor="cli")
        service.complete_job(job["job_id"], actor="cli")
        if state == "done":
            continue
        invoice = service.draft_invoice(job["job_id"], actor="cli")
        issue("invoice", invoice)
        if state == "paid":
            approve(service.request_payment(invoice["id"], "168.00", actor="cli",
                                            method="bank transfer", reference="FICTIONAL-PAID"))

    clock(now)
    receipts = data / "seed-fixtures"
    receipts.mkdir(parents=True, exist_ok=True)
    duplicate = False
    for index in range(5):
        photo = receipts / f"receipt-{index + 1}.png"
        photo.write_bytes(fixture.read_bytes() + f"\nFictional receipt variant {index}".encode())
        fields = {"vendor": ["Atlas Parts", "Sakura Supply", "Luna Hardware", "Kora Tools", "Sol Materials"][index],
                  "date": (now - timedelta(days=index)).date().isoformat(), "total": str(12 * (index + 1)),
                  "currency": "USD", "confidence": "high"}
        expense = service.propose_receipt(photo, fields, actor="cli", job=jobs[index])
        ticket = service.request_expense_confirmation(expense["id"], actor="cli")
        if index < 4:
            approve(ticket)
        if index == 0:
            duplicate = service.propose_receipt(photo, fields, actor="cli", job=jobs[0])["duplicate"]
    for action in queries.due_actions(now):
        if action["kind"] == "payment_chaser":
            service.enqueue_message(**action, now=now)
    summary = {
        "clock": now.isoformat(), "jobs": len(queries.jobs()), "states": queries.pipeline(),
        "expenses": len(queries.expenses()), "pending_approvals": len(queries.pending_approvals(now)),
        "duplicate_receipt_deduped": duplicate,
    }
    if summary["pending_approvals"] != 3:
        raise ValueError(f"Expected three pending approvals, got {summary['pending_approvals']}.")
    marker.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Confirm deletion of an unmarked demo ledger.")
    parser.add_argument("--reset", action="store_true", help="Replace only Chalkline data in workspace chalkline-dev.")
    parser.add_argument("--profile", choices=("chalkline-dev", "chalkline-ui"), default="chalkline-ui")
    args = parser.parse_args()
    source = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent"
    sys.path[:0] = [str(REPO), str(source), str(WORKSPACE / ".runtime" / "offline-deps")]
    original = {key: os.environ.get(key) for key in ("HERMES_HOME", "CHALKLINE_TEST_NOW")}
    try:
        print(json.dumps(seed(reset=args.reset, profile_name=args.profile, force=args.force), ensure_ascii=False, indent=2))
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    main()
