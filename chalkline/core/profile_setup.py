"""Restrict the dedicated business profile to office tools."""

from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml
from hermes_constants import get_hermes_home

from .. import SLUG


def restrict_toolsets(*, confirmed: bool = False) -> dict:
    """Persist the explicit office-only selection in the active profile's config."""
    home = get_hermes_home()
    if home.name == "default" or home.parent.name != "profiles" or not confirmed:
        raise ValueError("Use a dedicated named business profile and confirm restrict_toolsets=true.")
    path = home / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("The profile config must contain a YAML mapping.")
    platforms = config.setdefault("platform_toolsets", {})
    if not isinstance(platforms, dict):
        raise ValueError("platform_toolsets must contain a YAML mapping.")
    for surface in ("whatsapp", "telegram", "cli", "cron"):
        platforms[surface] = [SLUG]
    path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    if not path.is_file() or path.read_text(encoding="utf-8") != content:
        if path.exists():
            import shutil
            backup = path.with_suffix(".yaml.bak")
            if not backup.exists():
                shutil.copy2(path, backup)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            stream.write(content)
            temporary = Path(stream.name)
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"platform_toolsets": {key: platforms[key] for key in ("whatsapp", "telegram", "cli", "cron")},
            "restart_required": True}
