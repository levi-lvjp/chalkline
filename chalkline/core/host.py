"""Authorize office handlers from Hermes's documented gateway and turn hooks."""

import os
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from importlib import import_module
from threading import Lock

from .. import NAME
from .queries import is_owner, settings


def optional_channel(name: str):
    """Resolve a channels module when the channel package is installed."""
    base = __package__.rsplit(".", 1)[0] + ".channels"
    try:
        return import_module(f"{base}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name not in (base, f"{base}.{name}"):
            raise
        return None


@dataclass(frozen=True)
class Origin:
    platform: str
    user: str
    chat: str
    chat_type: str
    message_id: str
    text_only: bool


def _verified_actor(origin: Origin) -> str:
    if origin.platform == "whatsapp":
        identity = optional_channel("identity")
        phone = identity.dm_phone({"user_id": origin.user, "chat_id": origin.chat,
                                   "chat_type": origin.chat_type}) if identity else None
        if phone and is_owner(phone):
            return phone
    if origin.platform == "telegram" and origin.chat_type in ("dm", "private") and origin.user and origin.chat:
        current = settings()
        if (origin.user, origin.chat) == (current["telegram_owner_user_id"], current["telegram_owner_chat_id"]):
            return f"telegram:{origin.user}:{origin.chat}"
    raise PermissionError(f"{NAME} tools and commands require the configured owner.")


def _local_cli() -> bool:
    return os.environ.get("HERMES_INTERACTIVE") == "1" or os.environ.get("HERMES_SINGLE_QUERY_SESSION") == "1"


class OfficeHost:
    """Retain host-originated routing proof without reading Hermes internals."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._gateway_seen = False
        self._origin: ContextVar[Origin | None] = ContextVar("chalkline_origin", default=None)
        self._turns: OrderedDict[tuple[str, str], Origin | str] = OrderedDict()
        self._lock = Lock()

    def register(self) -> None:
        """Register the normalized event and turn hooks used by the guards."""
        self.ctx.register_hook("pre_gateway_dispatch", self.observe_gateway)
        self.ctx.register_hook("pre_llm_call", self.observe_turn)
        self.ctx.register_hook("on_session_end", self.forget_session)

    def observe_gateway(self, event, **_context) -> None:
        self._gateway_seen = True
        self._origin.set(None)
        source = event.source
        if source is None or (event.metadata or {}).get("whatsapp_from_owner"):
            return
        platform = getattr(source.platform, "value", source.platform)
        self._origin.set(Origin(
            str(platform or ""), str(source.user_id or ""), str(source.chat_id or ""),
            str(source.chat_type or ""), str(source.message_id or event.message_id or ""),
            bool(event.text and not event.media_urls and getattr(event.message_type, "value", "") == "text"),
        ))

    def observe_turn(
        self, session_id: str, task_id: str, platform: str, sender_id: str = "",
        parent_session_id: str = "", **_context,
    ) -> None:
        if not session_id or not task_id:
            return
        key = (session_id, task_id)
        with self._lock:
            self._turns.pop(key, None)
        if parent_session_id:
            return
        origin = self._origin.get()
        if origin is not None and platform == origin.platform and str(sender_id) == origin.user and origin.user:
            proof = origin
        elif platform == "cli" and _local_cli() and not self._gateway_seen:
            proof = "cli"
        else:
            return
        with self._lock:
            self._turns[key] = proof
            self._turns.move_to_end(key)
            if len(self._turns) > 256:
                self._turns.popitem(last=False)

    def forget_session(self, session_id: str, **_context) -> None:
        with self._lock:
            for key in tuple(self._turns):
                if key[0] == session_id:
                    del self._turns[key]

    def tool_actor(self, session_id: str | None, task_id: str | None) -> tuple[str, Origin | None]:
        """Verify the exact host-observed agent turn before dispatching a tool."""
        if self.ctx.current_cron_execution() is not None:
            raise PermissionError("Owner access is unavailable to scheduled runs.")
        with self._lock:
            proof = self._turns.get((session_id, task_id))
        if proof == "cli":
            return "cli", None
        if isinstance(proof, Origin):
            return _verified_actor(proof), proof
        raise PermissionError(f"{NAME} tools and commands require the configured owner.")

    def command_actor(self) -> str:
        """Authorize a literal command from the interactive CLI or a verified owner DM."""
        if self.ctx.current_cron_execution() is not None:
            raise PermissionError("Owner access is unavailable to scheduled runs.")
        origin = self._origin.get()
        if origin is not None:
            return _verified_actor(origin)
        if not self._gateway_seen and os.environ.get("HERMES_INTERACTIVE") == "1":
            return "cli"
        raise PermissionError(f"{NAME} tools and commands require the configured owner.")
