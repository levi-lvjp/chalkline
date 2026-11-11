"""Typed owner commands. Approval is deliberately absent from the agent toolset."""

import json

from .. import NAME
from . import queries, service
from .host import OfficeHost, optional_channel
from .approvals import template_text
from .validation import TEMPLATES


DEFAULT_TEMPLATES = {
    "enquiry_ack": "Thanks for contacting {business_name}. We have received your enquiry.",
    "appointment_reminder": "Reminder from {business_name}: your appointment is at {appointment_start}.",
    "review_request": "Thanks for choosing {business_name}. If you have a moment, leave us a review: {review_link}",
}
USAGE = (
    "/office setup | pending | approve <code> | reject <code> | jobs | job <J-0001> | "
    "export <from> <to> | digest"
)
SETUP_EXAMPLE = {
    "restrict_toolsets": True,
    "business_name": "Example Repairs", "owner_phones": ["+12025550101"], "currency": "GBP",
    "locale": "en-GB", "timezone": "Europe/London", "hourly_rate": "65.00",
    "material_billing_rule": "cost", "material_markup_pct": "0", "tax_rate": "0",
    "payment_terms_days": 30, "payment_text": "", "review_link": "", "digest_time": "17:00",
}


def _setup(ctx, raw: str, actor: str) -> dict:
    if actor != "cli":
        raise PermissionError("Run setup in the Hermes CLI.")
    if not raw:
        return {"usage": "/office setup <JSON object>", "example": SETUP_EXAMPLE,
                "settings": queries.settings(), "template_keys": list(TEMPLATES)}
    changes = json.loads(raw)
    if not isinstance(changes, dict):
        raise ValueError("Setup takes a JSON object.")
    old = queries.settings()
    templates = changes.pop("templates", DEFAULT_TEMPLATES if not old["business_name"] else {})
    if not isinstance(templates, dict):
        raise ValueError("Templates must be an object keyed by template kind.")
    templates = {kind: template_text(kind, text) for kind, text in templates.items()}
    from .profile_setup import restrict_toolsets

    confirmed = changes.pop("restrict_toolsets", False)
    if confirmed is not True:
        raise ValueError("Confirm restriction to office tools in this dedicated business profile with restrict_toolsets=true.")
    from hermes_constants import get_hermes_home
    if get_hermes_home().parent.name != "profiles":
        raise ValueError("Use a dedicated named business profile.")
    settings = service.configure(changes, actor=actor)
    profile = restrict_toolsets(confirmed=True)
    approvals = [service.propose_template(kind, text, actor=actor) for kind, text in templates.items()]
    tick = optional_channel("tick")
    if tick is None:
        schedule = {"status": "channels_unavailable"}
    else:
        schedule = tick.ensure_tick(ctx)
    return {"settings": settings, "template_approvals": approvals, "tick": schedule, "profile": profile}


def _resolve_code(action: str, code: str, actor: str) -> dict:
    if actor != "cli":
        raise PermissionError("Use the owner WhatsApp approval text or Telegram approval button.")
    if action == "reject":
        return service.reject(code, actor=actor)
    result = service.approve(code, actor=actor)
    if result["outbox_id"] is not None:
        outbound = optional_channel("outbound")
        if outbound:
            outcome = outbound.deliver_outbox(result["outbox_id"])
            result["delivery"] = outcome or queries.get_outbox(result["outbox_id"])["status"]
        else:
            result["delivery"] = "queued"
    return result


def _dispatch(ctx, raw_args: str, actor: str) -> object:
    parts = raw_args.strip().split(maxsplit=1)
    action, raw = (parts + ["", ""])[:2]
    args = raw.split()
    if action == "setup":
        return _setup(ctx, raw, actor)
    if action == "pending" and not args:
        return queries.pending_approvals()
    if action in ("approve", "reject") and len(args) == 1:
        return _resolve_code(action, args[0], actor)
    if action == "jobs" and not args:
        return queries.jobs()
    if action == "job" and len(args) == 1:
        return queries.job_detail(args[0])
    if action == "export" and len(args) == 2:
        return service.export_books(*args)
    if action == "digest" and not args:
        digest = optional_channel("digest")
        return digest.build_digest() if digest else queries.digest()
    if action == "invoice" and len(args) == 2 and args[0] in ("hold", "resume", "void"):
        status = {"hold": "on_hold", "resume": "sent", "void": "void"}[args[0]]
        return service.set_invoice_status(args[1], status, actor=actor)
    if action == "cancel" and len(args) == 1:
        return service.cancel_job(args[0], actor=actor)
    if action == "receipt-correct":
        if actor != "cli":
            raise PermissionError("Correct receipt fields from the Hermes CLI.")
        values = json.loads(raw)
        if not isinstance(values, dict):
            raise ValueError("Receipt correction takes a JSON object.")
        expense = service.correct_expense(actor=actor, **values)
        return {"expense": expense, "approval": service.request_expense_confirmation(expense["id"], actor=actor)}
    return USAGE


def register_commands(ctx, host: OfficeHost) -> None:
    """Register the typed /office command family."""

    def office(raw_args: str) -> str:
        try:
            result = _dispatch(ctx, raw_args, host.command_actor())
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
        except (ValueError, PermissionError, OSError, RuntimeError, TypeError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    ctx.register_command("office", handler=office, description=f"Manage {NAME} and review approvals",
                         args_hint="<setup|pending|approve|reject|jobs|job|export|digest>")
