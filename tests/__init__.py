"""The test package. Importing it installs the suite-wide guard (see network_guard.py): the real credentials are
blanked and any real outbound request raises. `python -m unittest discover -s tests` loads
test_network_guard.py (which imports it), and every module that imports `tests.<anything>` imports this first."""

from tests import network_guard  # noqa: F401

# The plan approval message's live lookups (fee option from HubSpot, room from Famly) are off in tests; the
# tests of those lookups pass fake clients (tests/test_plan_message.py).
from integrations import plan_facts as _plan_facts  # noqa: E402

_plan_facts.LIVE_LOOKUPS = False
