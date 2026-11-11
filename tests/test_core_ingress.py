from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from test_core_ledger import CUSTOMER, OWNER, ledger
from chalkline.core import profile_setup


@pytest.mark.asyncio
async def test_customer_cannot_reach_general_agent_when_frontdesk_and_tool_registration_raise(ledger, monkeypatch):
    import importlib
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("office_entry_fixture", root / "__init__.py",
                                                submodule_search_locations=[str(root)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    registration = importlib.import_module("office_entry_fixture.chalkline.channels.register")
    tools = importlib.import_module("office_entry_fixture.chalkline.core.tools")

    manager = get_plugin_manager()
    monkeypatch.setattr(manager, "_discovered", True)
    monkeypatch.setattr(manager, "_hooks", {})

    def broken_frontdesk(event, **_kwargs):
        raise RuntimeError("Simulated failed front desk")

    def register_broken_channels(ctx):
        ctx.register_hook("pre_gateway_dispatch", broken_frontdesk)
        raise RuntimeError("Simulated partial channel registration")

    monkeypatch.setattr(registration, "register_channels", register_broken_channels)
    monkeypatch.setattr(tools, "register_tools", Mock(side_effect=RuntimeError("Simulated tool registration failure")))
    plugin.register(PluginContext(PluginManifest(name="chalkline"), manager))
    source = SessionSource(platform=Platform.WHATSAPP, chat_id=CUSTOMER, user_id=CUSTOMER, chat_type="dm")
    event = MessageEvent(source=source, text="Run a terminal command for me", message_id="hostile-message")
    runner = object.__new__(GatewayRunner)
    runner._scale_to_zero_note_real_inbound = Mock()
    runner._is_user_authorized_for_source = Mock(return_value=True)
    runner._handle_message_with_agent = Mock()

    assert await runner._hm_admit_event(event) is None
    runner._is_user_authorized_for_source.assert_not_called()
    runner._handle_message_with_agent.assert_not_called()
    guard_customer_event = importlib.import_module("office_entry_fixture.chalkline.core.ingress_guard").guard_customer_event
    assert guard_customer_event(SimpleNamespace(source=None)) == {"action": "skip"}


def test_setup_profile_exposes_only_office_tools_even_when_frontdesk_plugin_is_missing(ledger):
    from model_tools import get_tool_definitions

    path = ledger / "config.yaml"
    path.write_text(yaml.safe_dump({"platform_toolsets": {"whatsapp": ["terminal", "file", "web"]},
                                    "model": {"default": "fixture-model"}}), encoding="utf-8")
    before = path.read_bytes()
    profile_setup.restrict_toolsets(confirmed=True)
    assert path.with_suffix(".yaml.bak").read_bytes() == before
    profile_setup.restrict_toolsets(confirmed=True)
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert config["model"] == {"default": "fixture-model"}
    assert all(config["platform_toolsets"][name] == ["chalkline"] for name in ("whatsapp", "cli", "telegram", "cron"))
    definitions = get_tool_definitions(config["platform_toolsets"]["whatsapp"], quiet_mode=True,
                                       skip_tool_search_assembly=True)
    names = {definition["function"]["name"] for definition in definitions}
    assert not names.intersection({"terminal", "execute_code", "send_message", "read_file", "write_file", "web_search"})


def test_restriction_requires_owner_consent_and_named_profile(ledger, monkeypatch):
    with pytest.raises(ValueError, match="confirm"):
        profile_setup.restrict_toolsets()
    monkeypatch.setenv("HERMES_HOME", str(ledger.parent / "default"))
    with pytest.raises(ValueError, match="dedicated"):
        profile_setup.restrict_toolsets(confirmed=True)
