import json
from types import SimpleNamespace

from gateway.config import Platform
from chalkline.core.tools import get_image_cache_dir
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource

from test_core_ledger import CUSTOMER, OWNER, ledger
from chalkline.core import commands, queries, service, tools
from chalkline.core.host import OfficeHost


class Context:
    def __init__(self):
        self.tools = {}
        self.commands = {}
        self.hooks = {}
        host = OfficeHost(self)
        host.register()
        tools.register_tools(self, host)
        commands.register_commands(self, host)

    def register_tool(self, name, handler, schema, **_kwargs):
        self.tools[name] = (handler, schema)

    def register_command(self, name, handler, **_kwargs):
        self.commands[name] = handler

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def current_cron_execution(self):
        return None

    def call(self, name, args):
        return json.loads(self.tools[name][0](args, session_id="session-fixture", task_id="task-fixture"))

    def observe_turn(self, platform, sender="", kind=MessageType.TEXT, message="fixture-message", chat_type="dm"):
        if platform == "whatsapp":
            source = SessionSource(platform=Platform.WHATSAPP, chat_type=chat_type, chat_id=sender,
                                   user_id=sender, message_id=message)
            event = MessageEvent(text="Owner text", source=source, message_type=kind, message_id=message)
            self.hooks["pre_gateway_dispatch"](event)
        self.hooks["pre_llm_call"](session_id="session-fixture", task_id="task-fixture", platform=platform, sender_id=sender)


def test_customer_cannot_use_tools_or_slash_commands(ledger):
    ctx = Context()
    assert "office_approve" not in ctx.tools
    assert len(ctx.tools) == 13
    ctx.observe_turn("whatsapp", CUSTOMER)
    result = ctx.call("office_capture", {"name": "Fictional Customer", "text": "New job", "actor": OWNER})
    assert "require the configured owner" in result["error"]
    assert "require the configured owner" in json.loads(ctx.commands["office"]("approve A1"))["error"]
    ctx.observe_turn("whatsapp", OWNER, chat_type="group")
    assert "error" in ctx.call("office_query", {"kind": "jobs"})
    assert queries.jobs() == []


def test_payment_requires_observed_owner_text(ledger):
    ctx = Context()
    job = service.capture_owner(actor=OWNER, name="Fictional Customer", text="Repair a tap.")["job_id"]
    service.complete_job(job, actor=OWNER, items=[{"description": "Repair", "unit_price": "20"}])
    invoice = service.draft_invoice(job, actor=OWNER)
    issue = service.request_issue("invoice", invoice["id"], actor=OWNER)
    service.approve(issue["code"], actor="cli")
    args = {"invoice": invoice["number"], "amount": "21.00"}

    ctx.observe_turn("whatsapp", OWNER, MessageType.VOICE, "voice-1")
    assert "text-only" in ctx.call("office_payment_record", args)["error"]
    ctx.observe_turn("whatsapp", OWNER, MessageType.TEXT, "text-1")
    assert ctx.call("office_payment_record", args)["code"].startswith("A")
    assert queries.job_detail(job)["payments"] == []
    ctx.hooks["on_session_end"](session_id="session-fixture")
    assert "error" in ctx.call("office_payment_record", args)


def test_receipt_uses_structured_vision_and_requests_confirmation(ledger, monkeypatch):
    calls = []
    ctx = Context()
    fields = {"vendor": "Mint Sprocket", "date": "2026-10-28", "total": 23.65,
              "currency": "CAD", "confidence": "high"}

    def structured(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content_type="json", parsed=fields)

    ctx.llm = SimpleNamespace(complete_structured=structured)
    path = get_image_cache_dir() / "receipt.png"
    path.write_bytes(b"fictional image")
    outside = ledger / "outside.png"
    outside.write_bytes(b"fictional private file")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    ctx.observe_turn("cli")
    assert "media cache" in ctx.call("office_receipt", {"path": str(outside)})["error"]
    result = ctx.call("office_receipt", {"path": str(path)})
    assert "expense" in result, result
    assert len(calls) == 1 and calls[0]["input"][0]["type"] == "image"
    assert result["expense"]["amount_minor"] == 2365
    assert result["expense"]["status"] == "proposed"
    assert result["approval"]["code"].startswith("A")
