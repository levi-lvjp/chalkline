import argparse
import json
import subprocess
from contextlib import asynccontextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chalkline.channels import outbound
from chalkline.core import queries, service


NUMBER = "+12025550109"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "chalkline-dev"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    service.configure({"business_name": "Mint Tap Repairs", "owner_phones": ["+12025550101"]}, actor="cli")
    return home


@pytest.mark.parametrize(("returncode", "payload", "outcome"), [
    (0, {"success": True}, "sent"),
    (1, {"error": "WhatsApp bridge error (400): bad number"}, "failed"),
    (1, {"error": "WhatsApp send failed: "}, "unknown"),
    (0, {"skipped": True}, "failed"),
    (1, {"success": False, "partial_success": True, "error": "Media dropped"}, "unknown"),
    (0, None, "unknown"),
])
def test_cli_outcome_mapping(profile, monkeypatch, returncode, payload, outcome):
    fake = Mock(return_value=subprocess.CompletedProcess([], returncode, json.dumps(payload)))
    monkeypatch.setattr(outbound.subprocess, "run", fake)
    assert outbound._send_whatsapp(NUMBER, "Fictional reminder")[0] == outcome
    assert fake.call_args.kwargs["env"]["HERMES_HOME"] == str(profile.parent.parent)


def test_timeout_is_unknown_and_row_is_never_reclaimed(monkeypatch, profile):
    row = service.enqueue_message("other", NUMBER, "Fictional reminder", "test:timeout")
    cli = Mock(side_effect=subprocess.TimeoutExpired("hermes", outbound.SEND_TIMEOUT))
    monkeypatch.setattr(outbound.subprocess, "run", cli)
    assert outbound.deliver_outbox(row["id"]) is None
    cli.assert_not_called()
    service.approve(row["code"], actor="cli")
    assert outbound.deliver_outbox(row["id"]) == "unknown"
    assert queries.get_outbox(row["id"])["status"] == "unknown"
    assert outbound.deliver_outbox(row["id"]) is None
    assert cli.call_count == 2
    assert cli.call_args_list[1].args[0][5] == "whatsapp:+12025550101"


def test_approved_text_and_pdf_use_real_cli_handler_and_mock_bridge(profile, monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from hermes_cli.send_cmd import cmd_send
    from plugins.plugin_storage import plugin_data_dir

    document = plugin_data_dir("chalkline") / "invoice fixture.pdf"
    pdf = b"%PDF-1.4\n% fictional invoice\n%%EOF\n"
    document.write_bytes(pdf)
    config = GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)})
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr("tools.send_message_tool._mirror_sent_message", lambda *_args: False)
    calls = []

    @asynccontextmanager
    async def respond(url, json=None, **_kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        calls.append((endpoint, json))
        if endpoint == "send-media":
            assert Path(json["filePath"]).read_bytes() == pdf

        async def body():
            return {"messageId": f"mock-{endpoint}"} if json else {}

        yield SimpleNamespace(status=200, json=body)

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(get=respond, post=respond)

    def run_cli(argv, *, input, timeout, **_kwargs):
        assert argv[:5] == ["hermes", "-p", "chalkline-dev", "send", "--to"]
        assert argv[5] in {f"whatsapp:{NUMBER}", "whatsapp:+12025550101"}
        assert argv[6:] == ["--json", "--file", "-"]
        assert timeout == outbound.SEND_TIMEOUT
        stdout = StringIO()
        with monkeypatch.context() as context, redirect_stdout(stdout), pytest.raises(SystemExit) as exit_info:
            context.setattr("sys.stdin", StringIO(input))
            cmd_send(argparse.Namespace(to=argv[5], message=None, file="-", json=True))
        return subprocess.CompletedProcess(argv, exit_info.value.code, stdout.getvalue())

    monkeypatch.setattr("aiohttp.ClientSession", session)
    monkeypatch.setattr(outbound.subprocess, "run", run_cli)
    text_row = service.enqueue_message("other", NUMBER, "Fictional reminder", "test:text")
    pdf_row = service.enqueue_message("other", NUMBER, "Fictional invoice", "test:pdf", attachment_path=document)
    for row in (text_row, pdf_row):
        service.approve(row["code"], actor="cli")
        assert outbound.deliver_outbox(row["id"]) == "sent"
        assert queries.get_outbox(row["id"])["status"] == "sent"
    customer = [(endpoint, payload) for endpoint, payload in calls
                if payload and payload["chatId"] == "12025550109@s.whatsapp.net"]
    assert [endpoint for endpoint, _ in customer] == ["send", "send-media"]
    assert customer[0][1] == {"chatId": "12025550109@s.whatsapp.net", "message": "Fictional reminder"}
    media = customer[1][1]
    assert media["mediaType"] == "document"
    assert media["caption"] == "Fictional invoice"
    staged = Path(media["filePath"])
    assert staged.is_relative_to(profile / "cache" / "scratch")
    assert not staged.exists()


def test_pdf_changed_after_claim_is_not_sent(profile, monkeypatch):
    from plugins.plugin_storage import plugin_data_dir

    document = plugin_data_dir("chalkline") / "changed.pdf"
    document.write_bytes(b"%PDF-1.4\n% approved\n%%EOF\n")
    row = service.enqueue_message("other", NUMBER, "Fictional invoice", "test:changed", attachment_path=document)
    service.approve(row["code"], actor="cli")
    real_claim = service.claim_outbox

    def claim_then_change(outbox_id, *, now):
        claim = real_claim(outbox_id, now=now)
        document.write_bytes(b"%PDF-1.4\n% modified\n%%EOF\n")
        return claim

    send = Mock()
    monkeypatch.setattr(service, "claim_outbox", claim_then_change)
    monkeypatch.setattr(outbound.subprocess, "run", send)
    assert outbound.deliver_outbox(row["id"]) == "failed"
    send.assert_called_once()
    assert send.call_args.args[0][5] == "whatsapp:+12025550101"


def test_text_cannot_add_unapproved_attachment(monkeypatch):
    cli = Mock()
    monkeypatch.setattr(outbound.subprocess, "run", cli)
    assert outbound._send_whatsapp(NUMBER, "Please see media:/other.pdf")[0] == "failed"
    cli.assert_not_called()
