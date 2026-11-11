from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from chalkline.core import queries, service
from chalkline.core.schema import connect
from chalkline.core.states import transition


OWNER = "+12025550101"
CUSTOMER = "+12025550109"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "chalkline-dev"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    service.configure({
        "business_name": "Mint Tap Repairs", "owner_phones": [OWNER], "currency": "CAD",
        "timezone": "America/Toronto", "hourly_rate": "65.00",
        "material_billing_rule": "cost_plus_pct", "material_markup_pct": "20", "tax_rate": "5",
    }, actor="cli")
    return home


def make_job(name="Fictional Customer"):
    return service.capture_owner(actor=OWNER, name=name, text="Fix a leaking tap.")["job_id"]


def test_inbound_dedupe_sources_restart_and_identity(ledger):
    photo = ledger / "fixture.png"
    audio = ledger / "fixture.ogg"
    photo.write_bytes(b"fictional photo")
    audio.write_bytes(b"fictional voice")
    first = service.capture_enquiry(CUSTOMER, name="Fictional Customer", text="Tap leaking", message_id="in-1",
                                    media=[{"kind": "photo", "path": photo}, {"kind": "voice", "path": audio}])
    duplicate = service.capture_enquiry(CUSTOMER, text="Tap leaking", message_id="in-1")
    assert duplicate == {key: first[key] for key in duplicate} | {"duplicate": True}
    service.capture_enquiry(CUSTOMER, message_id="in-2", media=[{"kind": "photo", "path": photo}])
    photo.unlink()
    audio.unlink()
    detail = queries.job_detail(first["job_number"])
    assert len(detail["sources"]) == 3
    assert detail["sources"][2]["text"] == "voice note, not transcribed"
    assert all(str(ledger / "plugin-data") in row["file_path"] for row in detail["sources"][1:])
    assert queries.is_owner(OWNER)
    with pytest.raises(PermissionError):
        service.capture_owner(actor=CUSTOMER, name="Fictional", text="Try owner mutation.")


def test_receipt_dedupe_and_material_billing(ledger):
    job, other = make_job(), make_job("Another Fictional Customer")
    fields = {"vendor": "Mint Sprocket", "date": "2026-10-28", "total": "23.65",
              "currency": "CAD", "confidence": "high"}
    first_path, second_path = ledger / "receipt.png", ledger / "other.png"
    first_path.write_bytes(b"fictional receipt A")
    second_path.write_bytes(b"fictional receipt B")
    first = service.propose_receipt(first_path, fields, job=job, actor=OWNER)
    same = service.propose_receipt(first_path, fields, job=other, actor=OWNER)
    assert same["duplicate"] and same["id"] == first["id"] and same["job_id"] == job
    near = service.propose_receipt(second_path, fields | {"vendor": "MINT  SPROCKET"}, job=other, actor=OWNER)
    assert near["id"] != first["id"] and near["possible_duplicates"][0]["id"] == first["id"]

    items = [{"description": "Parts", "kind": "material", "expense_id": first["id"]}]
    with pytest.raises(ValueError, match="confirmed receipt"):
        service.draft_quote(job, items, actor=OWNER)
    with connect(write=True) as db:
        service._confirm_expense(db, first["id"], OWNER)
    quote = service.draft_quote(job, items, actor=OWNER)
    assert quote["subtotal_minor"] == 2838
    assert quote["total_minor"] == 2980
    with pytest.raises(ValueError, match="from this job"):
        service.draft_quote(other, items, actor=OWNER)


def test_agreement_evidence_and_snapshot(ledger):
    job = make_job()
    quote = service.draft_quote(job, [{"description": "Labour", "kind": "labour", "qty": "1.25"}], actor=OWNER)
    with connect(write=True) as db:
        transition(db, "quotes", quote["id"], "awaiting_approval")
        transition(db, "quotes", quote["id"], "sent")
        service._job_state(db, job, "quoted", OWNER)
    service.record_quote_decision(quote["id"], "accepted", actor=OWNER)
    unagreed = service.propose_extra(job, "Unagreed parts", "50", actor=OWNER)
    agreed = service.propose_extra(job, "Agreed replacement", "25", actor=OWNER)
    source = service.capture_owner(actor=OWNER, job=job, text="Customer agreed to the replacement.")["source_ids"][0]
    with connect(write=True) as db:
        service._record_extra_decision(db, agreed["id"], source, "agreed", OWNER)
    service.complete_job(job, actor=OWNER)
    invoice = service.draft_invoice(job, actor=OWNER)
    assert invoice["total_minor"] == 11156
    assert [line["description"] for line in invoice["lines"]] == ["Labour", "Agreed replacement"]
    assert queries.job_detail(job)["variations"][0]["id"] == unagreed["id"]
    assert queries.job_detail(job)["variations"][0]["status"] == "proposed"


def test_invoice_numbers_unique_under_concurrent_drafts(ledger):
    jobs = [make_job(f"Fictional Customer {index}") for index in range(12)]
    for job in jobs:
        service.complete_job(job, actor=OWNER, items=[{"description": "Repair", "unit_price": "20"}])

    def draft(job):
        return service.draft_invoice(job, actor=OWNER)["number"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        numbers = list(pool.map(draft, jobs + jobs))
    assert len(set(numbers)) == len(jobs)
    assert numbers[:len(jobs)] == numbers[len(jobs):]


def test_low_confidence_and_ambiguous_job_block_commitment(ledger):
    path = ledger / "fuzzy.png"
    path.write_bytes(b"fictional blurry receipt")
    fields = {"vendor": "Fictional Shop", "date": "2026-11-01", "total": 3,
              "currency": "CAD", "confidence": "low"}
    with pytest.raises(ValueError, match="matches"):
        service.propose_receipt(path, fields, job="Sarah", actor=OWNER)
    expense = service.propose_receipt(path, fields, actor=OWNER)
    with connect(write=True) as db, pytest.raises(ValueError, match="Low-confidence"):
        service._confirm_expense(db, expense["id"], OWNER)


def test_schedule_and_complete_accepted_work(ledger):
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    job = make_job()
    quote = service.draft_quote(job, [{"description": "Repair", "unit_price": 20}], actor=OWNER, now=now)
    with connect(write=True) as db:
        transition(db, "quotes", quote["id"], "awaiting_approval")
        transition(db, "quotes", quote["id"], "sent")
        service._job_state(db, job, "quoted", OWNER, now)
    service.record_quote_decision(quote["id"], "accepted", actor=OWNER, now=now)
    with pytest.raises(ValueError, match="ambiguous"):
        service.schedule(job, "2026-11-01T01:30", "2026-11-01T02:30", actor=OWNER, now=now)
    first = service.schedule(job, "2026-11-01T10:00", "2026-11-01T11:00", actor=OWNER, now=now)
    second = service.schedule(job, "2026-11-02T10:00", "2026-11-02T11:00", actor=OWNER, now=now)
    assert first["id"] != second["id"]
    service.complete_job(job, actor=OWNER)
    assert [row["status"] for row in queries.job_detail(job)["appointments"]] == ["cancelled", "completed"]
