"""Hermes entry point for Chalkline."""

import logging



def register(ctx):
    """Install customer barriers before importing or registering owner tools."""
    from .chalkline.core.ingress_guard import guard_customer_event
    ctx.register_hook("pre_gateway_dispatch", guard_customer_event)
    try:
        from .chalkline.core.host import optional_channel

        from hermes_constants import get_hermes_home
        if (get_hermes_home() / "scripts" / "chalkline-tick.py").is_file():
            optional_channel("tick")._install_script()
        channels = optional_channel("register")
        if channels is not None:
            channels.register_channels(ctx)
    except (Exception, SystemExit):
        logging.getLogger(__name__).exception("Customer capture unavailable. The ingress barrier remains active.")
    try:
        from .chalkline.core.commands import register_commands
        from .chalkline.core.host import OfficeHost
        from .chalkline.core.tools import register_tools

        host = OfficeHost(ctx)
        host.register()
        register_tools(ctx, host)
        register_commands(ctx, host)
    except (Exception, SystemExit):
        logging.getLogger(__name__).exception("Office handlers unavailable. The ingress barrier remains active.")
