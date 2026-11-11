import logging

from .frontdesk import pre_gateway_dispatch


def register_channels(ctx):
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    try:
        from .approval_intake import post_gateway_admission, telegram_factory

        ctx.register_hook("post_gateway_admission", post_gateway_admission)
        ctx.register_platform_handler("telegram", telegram_factory)
    except (Exception, SystemExit):
        logging.getLogger(__name__).exception("Optional approval handlers failed. Front desk remains active.")
