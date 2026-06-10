"""tradinetools — shared utilities for the tradinebotte ecosystem."""

import os

__version__ = "0.1.0"


def read_version_stamp(install_dir: str | None = None) -> str:
    """Return the deployed git commit hash for this bot instance.

    Priority:
      1. TRADINEBOTTE_VERSION env var (set by CI or manual override)
      2. version.stamp file in install_dir (written by deploy scripts at restart time)
      3. "unknown" fallback

    The version.stamp file contains a short git hash written by the deploy script
    immediately before restarting the bot process, so the running bot always knows
    which exact commit it is executing.
    """
    env_ver = os.environ.get("TRADINEBOTTE_VERSION", "").strip()
    if env_ver:
        return env_ver

    search_dirs = []
    if install_dir:
        search_dirs.append(install_dir)
    search_dirs.append(os.path.expanduser("~/tradinebotte"))
    search_dirs.append(os.getcwd())

    for d in search_dirs:
        stamp = os.path.join(d, "version.stamp")
        try:
            with open(stamp, encoding="utf-8") as f:
                ver = f.read().strip()
            if ver:
                return ver
        except OSError:
            continue

    return "unknown"
