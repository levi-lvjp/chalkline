import base64
import importlib.util
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chalkline.core import queries, service
from chalkline.ui.api import router


@pytest.fixture
def client(tmp_path, monkeypatch):
    workspace = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("ui_seed", Path(__file__).resolve().parents[1] / "scripts" / "demo_seed.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    monkeypatch.setattr(demo, "WORKSPACE", tmp_path)
    profile = tmp_path / ".runtime" / "profiles" / "chalkline-dev"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}")
    fixture = tmp_path / "plugin" / "fixtures"
    fixture.mkdir(parents=True)
    (fixture / "receipt.png").write_bytes((Path(__file__).resolve().parents[1] / "fixtures" / "receipt.png").read_bytes())
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("CHALKLINE_TEST_NOW", demo.DEFAULT_CLOCK)
    demo.seed(reset=True, profile_name='chalkline-dev')
    from hermes_cli import web_server
    app = FastAPI()
    app.middleware("http")(web_server.auth_middleware)
    app.include_router(router, prefix="/api/plugins/chalkline")
    test = TestClient(app)
    test.headers["Authorization"] = "Bearer " + web_server._SESSION_TOKEN
    return test


BASE = "/api/plugins/chalkline"


def test_all_routes_require_host_owner_auth(client):
    client.headers.clear()
    for method, route, body in [
        ("get", "/overview", None), ("get", "/jobs/J-0001", None),
        ("get", "/sources/1/thumbnail", None),
        ("post", "/export", {"from": "2026-11-01", "to": "2026-11-30"}),
        ("post", "/approvals/A1/approve", {}), ("post", "/approvals/A1/reject", {}),
        ("post", "/invoices/INV-0001/payments", {"amount": "1.00"}),
    ]:
        response = client.request(method, BASE + route, json=body)
        assert response.status_code == 401


def test_overview_detail_and_bounded_thumbnails(client):
    data = client.get(BASE + "/overview").json()
    assert sum(map(len, data["pipeline"].values())) == 12
    assert data["money"]["paid_this_month_minor"] == 16800
    assert data["money"]["expenses_this_month_minor"] == 12000
    assert len(data["appointments"]) == 2 and len(data["approvals"]) == 3
    assert all(isinstance(amount, int) for amount in data["money"].values())
    quote = next(row for row in data["approvals"] if row["kind"] == "quote")
    assert quote["amount_minor"] == 18000
    detail = client.get(BASE + "/jobs/J-0001").json()
    assert detail["timeline"]
    text = str(data) + str(detail)
    assert "file_path" not in text and "attachment_path" not in text and "plugin-data" not in text
    source = next(row for row in detail["sources"] if row["kind"] == "photo")
    thumbnail = client.get(BASE + source["thumbnail_url"])
    assert thumbnail.status_code == 200
    assert len(base64.b64decode(thumbnail.json()["data_url"].split(",", 1)[1])) < 100_000
    assert client.get(BASE + "/sources/999999/thumbnail").status_code == 404
    assert client.get(BASE + "/jobs/J-9999").status_code == 404


def test_export_approval_replay_expiry_rejection_and_payment(client, monkeypatch):
    exported = client.post(BASE + "/export", json={"from": "2026-11-01", "to": "2026-11-30"})
    assert exported.status_code == 200 and base64.b64decode(exported.json()["base64"]).startswith(b"PK")
    assert client.post(BASE + "/export", json={"from": "bad", "to": "bad"}).status_code == 400
    rows = queries.pending_approvals()
    row = next(row for row in rows if row["kind"] == "confirm_expense")
    assert client.post(BASE + f"/approvals/{row['code']}/approve").status_code == 200
    assert any(event["actor"] == "dashboard" for event in queries.job_events("J-0005"))
    assert client.post(BASE + f"/approvals/{row['code']}/approve").status_code == 409
    reject = next(row for row in rows if row["kind"] == "quote")
    assert client.post(BASE + f"/approvals/{reject['code']}/reject").status_code == 200
    invoice = queries.money_owed()[0]
    payment = client.post(BASE + f"/invoices/{invoice['number']}/payments",
                          json={"amount": "10.00", "reference": "Test owner payment"})
    assert payment.status_code == 200
    assert client.post(BASE + f"/approvals/{payment.json()['code']}/approve").status_code == 200
    expired = service.enqueue_message("other", "+12025550199", "Expiry fixture", "ui:expiry")
    monkeypatch.setenv("CHALKLINE_TEST_NOW", (queries.clock_now() + timedelta(hours=25)).isoformat())
    assert client.post(BASE + f"/approvals/{expired['code']}/approve").status_code == 409


def test_desktop_modules_parse_and_money_uses_currency_minor_units():
    desktop = Path(__file__).resolve().parents[1] / "desktop"
    for source in desktop.glob("*.js"):
        result = subprocess.run(["node", "--input-type=module", "--check"],
                                input=source.read_text(encoding="utf-8"), text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
    code = (desktop / "money.js").read_text(encoding="utf-8")
    code += '\nif (money(12345, "USD") !== "$123.45" || !money(123, "JPY").endsWith("123")) process.exit(1);'
    result = subprocess.run(["node", "--input-type=module"], input=code, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_dashboard_approval_attempts_delivery_immediately(client, monkeypatch):
    from unittest.mock import Mock
    from chalkline.channels import outbound

    delivery = Mock(return_value="sent")
    monkeypatch.setattr(outbound, "deliver_outbox", delivery)
    quote = next(row for row in queries.pending_approvals() if row["kind"] == "quote")
    response = client.post(BASE + "/approvals/" + quote["code"] + "/approve", json={})
    assert response.status_code == 200
    assert response.json()["delivery"] == "sent"
    delivery.assert_called_once_with(response.json()["outbox_id"])
