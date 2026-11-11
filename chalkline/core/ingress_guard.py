"""Independent gateway barrier retained when another registration or hook fails."""


def guard_customer_event(event, **_context):
    """Suppress every unverified WhatsApp event before the general agent can run."""
    try:
        source = getattr(event, "source", None)
        if source is None:
            return {"action": "skip"}
        platform = getattr(source.platform, "value", source.platform)
        if platform != "whatsapp":
            return None
        if (getattr(event, "metadata", None) or {}).get("whatsapp_from_owner"):
            return {"action": "skip"}
        from .host import optional_channel
        from .queries import is_owner

        identity = optional_channel("identity")
        phone = identity.dm_phone(source) if identity else None
        if phone and is_owner(phone):
            return None
    except (Exception, SystemExit):
        pass
    return {"action": "skip"}
