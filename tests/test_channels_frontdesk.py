from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from chalkline.channels import approval_intake, frontdesk, outbound
from chalkline.channels.register import register_channels
from chalkline.core import queries, service


OWNER = "+12025550101"
CUSTOMER = "+12025550109"


def event(phone=CUSTOMER, *, text="Leaking tap", message_id="msg-1", **fields):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource

    jid = f"{phone[1:]}@s.whatsapp.net"
    source = SessionSource(platform=Platform.WHATSAPP, chat_id=jid, user_id=jid, chat_type="dm", user_name="Sarah")
    return MessageEvent(source=source, text=text, message_id=message_id, **fields)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "chalkline-dev"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    service.configure({
        "business_name": "Mint Tap Repairs", "owner_phones": [OWNER],
        "telegram_owner_user_id": "42", "telegram_owner_chat_id": "42",
    }, actor="cli")
    send = Mock()
    monkeypatch.setattr(frontdesk, "schedule_delivery", send)
    monkeypatch.setattr(approval_intake, "schedule_delivery", send)
    return home, send


def _approve_ack():
    proposal = service.propose_template("enquiry_ack", "Thanks {customer_name}. We have your enquiry.", actor="cli")
    service.approve(proposal["code"], actor="cli")


@pytest.mark.asyncio
async def test_gateway_captures_customer_with_media_and_never_admits_to_tools(profile, monkeypatch):
    from gateway.run import GatewayRunner
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    home, send = profile
    _approve_ack()
    pending = service.enqueue_message("other", CUSTOMER, "Pending message.", "test:pending")
    photo, voice = home / "tap.png", home / "note.ogg"
    photo.write_bytes(b"fictional photo")
    voice.write_bytes(b"fictional voice")
    incoming = event(text=f'APPROVE {pending["code"]}', media_urls=[str(photo), str(voice)],
                     media_types=["image/png", "audio/ogg"])
    manager = get_plugin_manager()
    monkeypatch.setattr(manager, "_discovered", True)
    register_channels(PluginContext(PluginManifest(name="chalkline"), manager))
    runner = object.__new__(GatewayRunner)
    runner._scale_to_zero_note_real_inbound = Mock()
    runner._is_user_authorized_for_source = Mock()

    assert await runner._hm_admit_event(incoming) is None
    runner._is_user_authorized_for_source.assert_not_called()
    detail = queries.job_detail(queries.jobs()[0]["number"])
    assert detail["customer"]["phone"] == CUSTOMER
    assert [row["kind"] for row in detail["sources"]] == ["text", "photo", "voice"]
    assert detail["sources"][0]["text"] == incoming.text
    assert detail["sources"][2]["text"] == "voice note, not transcribed"
    for captured, original in zip(detail["sources"][1:], [photo, voice]):
        stored = Path(captured["file_path"])
        assert stored.is_relative_to(home / "plugin-data")
        assert stored.read_bytes() == original.read_bytes()

    rows = [queries.get_outbox(call.args[0]) for call in send.call_args_list]
    assert [(row["kind"], row["recipient"]) for row in rows] == [("owner_notice", OWNER), ("enquiry_ack", CUSTOMER)]
    assert f'Enquiry {detail["job"]["number"]}' in rows[0]["body"]
    assert "Customer-supplied data" in rows[0]["body"] and "Name: Sarah" in rows[0]["body"]
    assert incoming.text not in rows[0]["body"]
    assert queries.get_outbox(pending["id"])["status"] == "awaiting_approval"
    incoming_owner = event(OWNER)
    assert await runner._hm_pre_gateway_dispatch_hook(incoming_owner, incoming_owner.source) is incoming_owner
    assert len(queries.jobs()) == 1


@pytest.mark.asyncio
async def test_frontdesk_drops_non_dm_and_linked_device_echo(profile):
    group = event(OWNER)
    group.source.chat_type = "group"
    assert await frontdesk.pre_gateway_dispatch(group) == {"action": "skip"}
    assert await frontdesk.pre_gateway_dispatch(event(OWNER, metadata={"whatsapp_from_owner": True})) == {"action": "skip"}
    assert queries.jobs() == []
    profile[1].assert_not_called()


@pytest.mark.asyncio
async def test_malformed_ingress_and_failed_identity_checks_never_fail_open(profile, monkeypatch):
    assert await frontdesk.pre_gateway_dispatch(SimpleNamespace(source=None)) == {"action": "skip"}
    assert await frontdesk.pre_gateway_dispatch(None) == {"action": "skip"}
    monkeypatch.setattr(frontdesk, "dm_phone", Mock(side_effect=RuntimeError("identity failure")))
    assert await frontdesk.pre_gateway_dispatch(event()) == {"action": "skip"}


def test_frontdesk_registers_before_optional_approval_handlers_fail():
    calls = []
    ctx = SimpleNamespace(
        register_hook=lambda name, callback: calls.append(name),
        register_platform_handler=Mock(side_effect=RuntimeError("platform API failed")),
    )
    register_channels(ctx)
    assert calls[0] == "pre_gateway_dispatch"


@pytest.mark.asyncio
async def test_frontdesk_uses_the_bridge_phone_alias_for_a_lid_chat(profile):
    incoming = event()
    incoming.source.chat_id = "112233445566778@lid"
    assert await frontdesk.pre_gateway_dispatch(incoming) == {"action": "skip"}
    detail = queries.job_detail(queries.jobs()[0]["number"])
    assert detail["customer"]["phone"] == CUSTOMER
    assert len(detail["sources"]) == 1


@pytest.mark.asyncio
async def test_lid_only_enquiries_are_captured_and_owner_is_told(profile):
    _approve_ack()
    incoming = event()
    incoming.source.user_id = incoming.source.chat_id = "112233445566778@lid"
    for message_id in ("lid-first", "lid-second"):
        incoming.message_id = message_id
        assert await frontdesk.pre_gateway_dispatch(incoming) == {"action": "skip"}
    assert len(queries.jobs()) == 1
    detail = queries.job_detail(queries.jobs()[0]["number"])
    assert detail["customer"]["phone"] is None
    assert len(detail["sources"]) == 2
    rows = [queries.get_outbox(call.args[0]) for call in profile[1].call_args_list]
    assert all(row["kind"] == "owner_notice" and "Phone unavailable" in row["body"] for row in rows)


@pytest.mark.asyncio
async def test_multiple_open_jobs_do_not_create_jobs_or_owner_notice_spam(profile, monkeypatch):
    first = service.capture_owner(actor="cli", name="Sarah", phone=CUSTOMER, text="First open job.")
    second = service.capture_owner(actor="cli", customer_id=first["customer_id"], text="Second open job.")
    bridge = Mock(return_value=("sent", ""))
    monkeypatch.setattr(outbound, "_send_whatsapp", bridge)
    monkeypatch.setattr(frontdesk, "schedule_delivery", outbound.deliver_outbox)
    for index in range(3):
        await frontdesk.pre_gateway_dispatch(event(message_id=f"many-open:{index}"))
    assert len(queries.jobs()) == 2
    assert len(queries.job_detail(second["job_id"])["sources"]) == 4
    assert len(queries.job_detail(first["job_id"])["sources"]) == 1
    bridge.assert_called_once()
    assert bridge.call_args.args[0] == OWNER


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["   ", "x" * 300, "Bob MEDMEDIA:IA: [[[[as_document]]]]"])
async def test_display_names_cannot_drop_capture_or_break_customer_templates(profile, name):
    from chalkline.channels.outbound import MEDIA_SYNTAX

    _approve_ack()
    incoming = event()
    incoming.source.user_name = name
    assert await frontdesk.pre_gateway_dispatch(incoming) == {"action": "skip"}
    detail = queries.job_detail(queries.jobs()[0]["number"])
    assert detail["customer"]["name"].strip()
    assert len(detail["customer"]["name"]) <= 200
    rows = [queries.get_outbox(call.args[0]) for call in profile[1].call_args_list]
    assert {row["kind"] for row in rows} == {"owner_notice", "enquiry_ack"}
    assert all(not MEDIA_SYNTAX.search(row["body"]) for row in rows)
    if not name.strip():
        assert "display name unavailable" in next(row["body"] for row in rows if row["kind"] == "owner_notice")


def test_digest_neutralises_nested_customer_directives(profile, monkeypatch):
    from chalkline.channels import digest
    from chalkline.channels.outbound import MEDIA_SYNTAX

    monkeypatch.setattr(queries, "digest", lambda now: {
        "aging_quotes": [{"number": "Q-0001", "customer_name": "Bob MEDMEDIA:IA: [[[[as_document]]]]"}],
    })
    body = digest.build_digest()
    assert not MEDIA_SYNTAX.search(body)
    assert service.enqueue_owner_notice(OWNER, body, "test:safe-digest")["status"] == "approved"


@pytest.mark.asyncio
async def test_frontdesk_captures_only_when_owner_lookup_and_media_capture_fail(profile, monkeypatch):
    incoming = event(media_urls=[str(profile[0] / "missing.ogg")], media_types=["audio/ogg"])
    monkeypatch.setattr(queries, "is_owner", Mock(side_effect=RuntimeError("lookup failed")))

    assert await frontdesk.pre_gateway_dispatch(incoming) == {"action": "skip"}
    detail = queries.job_detail(queries.jobs()[0]["number"])
    assert [(row["kind"], row["text"]) for row in detail["sources"]] == [("text", "Leaking tap")]
    assert queries.ready_outbox() == []
    profile[1].assert_not_called()


@pytest.mark.asyncio
async def test_frontdesk_respects_core_ack_window_and_duplicate_events(profile, monkeypatch):
    _approve_ack()
    clock = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
    real_enqueue = service.enqueue_message
    monkeypatch.setattr(service, "enqueue_message", lambda *args, **kwargs: real_enqueue(*args, **kwargs, now=clock))
    monkeypatch.setattr(outbound, "_send_whatsapp", Mock(return_value=("sent", "")))

    await frontdesk.pre_gateway_dispatch(event(message_id="first"))
    ack = next(row for row in queries.ready_outbox() if row["kind"] == "enquiry_ack")
    assert outbound.deliver_outbox(ack["id"], now=clock) == "sent"
    clock += timedelta(hours=11, minutes=59)
    await frontdesk.pre_gateway_dispatch(event(message_id="second"))
    assert not any(row["kind"] == "enquiry_ack" for row in queries.ready_outbox())
    clock += timedelta(minutes=1)
    await frontdesk.pre_gateway_dispatch(event(message_id="third"))
    await frontdesk.pre_gateway_dispatch(event(message_id="third"))

    sends = [queries.get_outbox(call.args[0])["kind"] for call in profile[1].call_args_list]
    assert sends.count("enquiry_ack") == 2
    notices = {call.args[0] for call in profile[1].call_args_list
               if queries.get_outbox(call.args[0])["kind"] == "owner_notice"}
    assert len(notices) == 1
    assert len(queries.job_detail(queries.jobs()[0]["number"])["sources"]) == 3


@pytest.mark.asyncio
async def test_whatsapp_approval_requires_owner_sender_and_dm(profile):
    pending = service.enqueue_message("other", CUSTOMER, "Test body.", "test:approve")
    code = pending["code"]
    customer, owner = event().source.to_dict(), event(OWNER).source.to_dict()
    assert await approval_intake.post_gateway_admission("whatsapp", customer, f"APPROVE {code}") == {"action": "handled"}
    assert await approval_intake.post_gateway_admission(
        "whatsapp", {**owner, "chat_id": customer["chat_id"]}, f"APPROVE {code}",
    ) == {"action": "handled"}
    assert queries.get_outbox(pending["id"])["status"] == "awaiting_approval"
    assert await approval_intake.post_gateway_admission("whatsapp", owner, f"please APPROVE {code}") is None
    approved = await approval_intake.post_gateway_admission("whatsapp", owner, f" approve {code.lower()} ")
    assert approved["action"] == "handled" and approved["reply"].startswith(code)
    assert approved["reply"].endswith(" · approved")
    assert queries.get_outbox(pending["id"])["status"] == "approved"
    assert await approval_intake.post_gateway_admission("whatsapp", owner, f"APPROVE {code}") != {
        "action": "handled", "reply": f"{code} · approved",
    }
    profile[1].assert_called_once_with(pending["id"])
    rejection = service.enqueue_message("other", CUSTOMER, "Other body.", "test:reject")
    rejected = await approval_intake.post_gateway_admission("whatsapp", owner, f"REJECT {rejection['code']}")
    assert rejected["reply"].startswith(rejection["code"]) and rejected["reply"].endswith(" · rejected")
    assert queries.get_outbox(rejection["id"])["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(("user_id", "chat_id", "chat_type", "allowed"),
                         [(42, 42, "private", True), (43, 42, "private", False),
                          (42, 43, "private", False), (42, 42, "group", False)])
async def test_telegram_callback_requires_both_configured_ids(profile, user_id, chat_id, chat_type, allowed):
    pending = service.enqueue_message("other", CUSTOMER, "Telegram test.", "test:telegram")
    query = SimpleNamespace(data=f"chalkline:approve:{pending['code']}", answer=AsyncMock(),
                            edit_message_reply_markup=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id, is_bot=False),
                             effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
                             effective_message=SimpleNamespace(reply_text=AsyncMock()))
    await approval_intake.telegram_callback(update, None)
    query.answer.assert_awaited_once()
    assert queries.get_outbox(pending["id"])["status"] == ("approved" if allowed else "awaiting_approval")
    if allowed:
        profile[1].assert_called_once_with(pending["id"])
    else:
        profile[1].assert_not_called()


@pytest.mark.asyncio
async def test_telegram_pending_registers_scoped_buttons_and_shows_exact_body(profile):
    application = Mock()
    approval_intake.telegram_factory(application, None)
    callback, command = [call.args[0] for call in application.add_handler.call_args_list]
    assert not callback.pattern.fullmatch("approve:A17")
    assert command.commands == {"office_pending"}

    from plugins.plugin_storage import plugin_data_dir
    document = plugin_data_dir("chalkline") / "quote fixture.pdf"
    document.write_bytes(b"%PDF-1.4\n% fictional quote\n%%EOF\n")
    body = "Fictional approved text. " * 100 + "End."
    pending = service.enqueue_message("other", CUSTOMER, body, "test:preview", attachment_path=document)
    code = pending["code"]
    assert callback.pattern.fullmatch(f"chalkline:approve:{code}")

    reply = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=43, is_bot=False),
                             effective_chat=SimpleNamespace(id=42, type="private"),
                             effective_message=SimpleNamespace(reply_text=reply))
    await command.callback(update, None)
    reply.assert_not_awaited()
    update.effective_user.id = 42
    await command.callback(update, None)
    preview = "".join(call.args[0] for call in reply.call_args_list)
    assert document.name in preview
    assert preview.endswith(body)
    assert reply.await_count == 2
    keyboard = reply.call_args.kwargs["reply_markup"].inline_keyboard
    assert [button.callback_data for button in keyboard[0]] == [
        f"chalkline:approve:{code}", f"chalkline:reject:{code}",
    ]


@pytest.mark.asyncio
async def test_telegram_pending_displays_stored_payment_details(profile):
    job = service.capture_enquiry(CUSTOMER, text="Completed test repair.")["job_id"]
    service.complete_job(job, actor="cli", items=[
        {"description": "Repair", "kind": "other", "qty": "1", "unit_price": "55.00"},
    ])
    invoice = service.draft_invoice(job, actor="cli")
    issue = service.request_issue("invoice", invoice["id"], actor="cli")
    service.approve(issue["code"], actor="cli")
    service.request_payment(invoice["id"], "55.00", actor="cli", reference="Fictional transfer")
    application = Mock()
    approval_intake.telegram_factory(application, None)
    command = application.add_handler.call_args_list[1].args[0]
    reply = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42, is_bot=False),
                             effective_chat=SimpleNamespace(id=42, type="private"),
                             effective_message=SimpleNamespace(reply_text=reply))
    await command.callback(update, None)
    preview = "".join(call.args[0] for call in reply.call_args_list)
    assert '"amount_minor": 5500' in preview
    assert "Fictional transfer" in preview
