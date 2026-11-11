import json
import logging
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

import hermes_constants
from plugins.plugin_storage import plugin_db

from .. import SLUG
from ..core import queries, service
from .digest import queue_digest
from .outbound import deliver_outbox


logger = logging.getLogger(__name__)
JOB_NAME = f"{SLUG}-tick"
SCRIPT_NAME = f"{JOB_NAME}.py"
SCHEDULE = "*/15 * * * *"


def run_tick(*, now=None):
    """Queue due work and attempt each claimable send once."""
    now = now or queries.clock_now()
    recovered = service.recover_interrupted_sends(now)
    stats = {"queued": 0, "sent": 0, "failed": 0, "unknown": recovered, "errors": 0}
    try:
        actions = queries.due_actions(now)
    except Exception:
        logger.exception("Could not read scheduled customer actions.")
        stats["errors"] += 1
        actions = ()
    for action in actions:
        try:
            if service.enqueue_message(**action, now=now):
                stats["queued"] += 1
        except Exception:
            logger.exception("Could not queue scheduled customer action.")
            stats["errors"] += 1
    try:
        queue_digest(now)
    except Exception:
        logger.exception("Could not queue daily digest.")
        stats["errors"] += 1
    for row in queries.ready_outbox():
        try:
            outcome = deliver_outbox(row["id"], now=now)
            if outcome:
                stats[outcome] += 1
        except Exception:
            logger.exception("Could not deliver outbox row %s.", row["id"])
            stats["errors"] += 1
    return stats


def _install_script():
    directory = hermes_constants.get_hermes_home() / "scripts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SCRIPT_NAME
    content = """# Installed by /office setup.
import os
import sys
from pathlib import Path

home = Path(__file__).resolve().parent.parent
os.environ["HERMES_HOME"] = str(home)
sys.path.insert(0, str(home / "plugins" / "chalkline"))
from chalkline.channels.tick import run_tick

result = run_tick()
if result["errors"]:
    raise SystemExit("Chalkline tick recorded errors. Check the profile logs.")
"""
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as temp:
        temp.write(content)
    temp_path = Path(temp.name)
    try:
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def ensure_tick(ctx):
    """Create the profile's no-agent job once, including under concurrent setup calls."""

    from tools.registry import discover_builtin_tools
    discover_builtin_tools()

    def dispatch(**args):
        result = json.loads(ctx.dispatch_tool("cronjob_manage", args))
        if not result.get("success"):
            raise RuntimeError(result.get("error") or "Hermes cron could not configure the tick.")
        return result

    # A separate profile-local database provides a process-safe mutex for list/create.
    with closing(plugin_db(SLUG, "cron-setup.db")) as lock, lock:
        lock.execute("BEGIN IMMEDIATE")
        _install_script()
        listing = dispatch(action="list", include_disabled=True)
        jobs = [job for job in listing["jobs"] if job["name"] == JOB_NAME]
        if len(jobs) > 1:
            raise RuntimeError("Multiple Chalkline tick jobs exist in this profile.")
        if jobs:
            job = jobs[0]
            return {"status": "existing", "job_id": job["job_id"], "job": job}
        return dispatch(
            action="create", name=JOB_NAME, schedule=SCHEDULE, script=SCRIPT_NAME,
            no_agent=True, deliver="local",
        )
