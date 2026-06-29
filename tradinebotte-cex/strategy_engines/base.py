"""
Strategy protocol — re-exported from the neutral core.

The protocol now lives in `botcore.strategy` (Plan D step 3) so the core owns the
strategy seam and neither plugin owns it. This module keeps the historical import
path `from strategy_engines.base import Strategy` working.

`botcore/` is shipped flat beside `strategy_engines/` on a deployed account
(install/update copy it like `connectors/`), so `import botcore` resolves there.
In the monorepo it lives at the sibling `tradinebotte-core/`, added to sys.path
below — mirroring how live_bot reconciles the flat-deploy vs monorepo layouts.
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

from botcore.strategy import Strategy  # noqa: E402  pylint: disable=wrong-import-position

__all__ = ["Strategy"]
