"""Ad hoc pipeline configuration, read fresh from the environment on every call.

    ADHOC_MODE        off | dry | live   (default off; anything else is off)
        off   enqueue refuses and the worker does nothing.
        dry   the worker plans each deal (READ-ONLY) and records the plan; NO Famly or HubSpot write.
        live  NOT BUILT YET: the worker refuses to run in it (the writes arrive in a later step).
    ADHOC_STORE_PATH  optional path to the jobs SQLite file (default var/adhoc_jobs.sqlite3, gitignored).
"""

import logging
import os

logger = logging.getLogger(__name__)

MODE_OFF, MODE_DRY, MODE_LIVE = "off", "dry", "live"


def mode() -> str:
    raw = os.environ.get("ADHOC_MODE", "").strip().lower()
    if raw == "":
        return MODE_OFF
    if raw in (MODE_OFF, MODE_DRY, MODE_LIVE):
        return raw
    logger.error("ADHOC_MODE=%r is not off, dry or live; treating it as off", raw)
    return MODE_OFF
