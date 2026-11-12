"""Restrict the dedicated business profile to office tools."""

import shutil

from hermes_cli.config import get_config_path, load_config, save_config
from hermes_constants import get_hermes_home

from .. import SLUG

SURFACES = ("whatsapp", "telegram", "cli", "cron")


def restrict_toolsets(*, confirmed: bool = False) -> dict:
    """Persist the explicit office-only selection in the active profile's config."""
    home = get_hermes_home()
    if home.name == "default" or home.parent.name != "profiles" or not confirmed:
        raise ValueError("Use a dedicated named business profile and confirm restrict_toolsets=true.")
    config = load_config()
    platforms = config.get("platform_toolsets")
    if platforms is None:
        platforms = config["platform_toolsets"] = {}
    if not isinstance(platforms, dict):
        raise ValueError("platform_toolsets must contain a mapping.")
    if any(platforms.get(surface) != [SLUG] for surface in SURFACES):
        for surface in SURFACES:
            platforms[surface] = [SLUG]
        path = get_config_path()
        backup = path.with_suffix(".yaml.bak")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        # Write only platform_toolsets; merge_existing keeps every other on-disk section as the owner wrote it.
        save_config({"platform_toolsets": platforms}, merge_existing=True)
    return {"platform_toolsets": {key: platforms[key] for key in SURFACES}, "restart_required": True}
