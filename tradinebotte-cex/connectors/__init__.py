"""
Connector registry — re-exported from the neutral core.

The registry now lives in `botcore.connectors` (Plan D step 3) so the core owns the
connector-load interface and it no longer physically sits in the CEX package. This
module keeps the historical import path `from connectors import load` working for
cex_feed, the strategy engines, and tests.

`botcore/` is shipped flat beside this package on a deployed account (install/update
copy it like this one), so `import botcore` resolves there. In the monorepo it lives
at the sibling `tradinebotte-core/`, added to sys.path below — mirroring how
strategy_engines/base.py and live_bot reconcile the flat-deploy vs monorepo layouts.
"""

import os
import sys

# Monorepo: botcore lives at ../../tradinebotte-core relative to this file.
# Flat deploy: that directory does not exist here (botcore/ sits in INSTALL_DIR,
# already on sys.path), so the insert is skipped and `import botcore` resolves there.
_CORE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tradinebotte-core")
)
if os.path.isdir(_CORE_DIR) and _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

# Re-export the public surface so `from connectors import load` etc. work.
from botcore.connectors import (  # noqa: E402  pylint: disable=wrong-import-position
    load, validate, available,
)

__all__ = ["load", "validate", "available"]
