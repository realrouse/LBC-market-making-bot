"""Logging setup shared across all tradinebotte services."""

import logging
import logging.handlers
import sys


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

_BOT_FORMAT  = "%(asctime)s %(levelname)s %(message)s"
_BOT_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_root_logger(log_path: str, max_bytes: int = 5_000_000) -> None:
    """Configure the root logger with rotating file output and optional TTY console.

    Captures all loggers (including library logs) — suited for standalone bot processes.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(_BOT_FORMAT, datefmt=_BOT_DATEFMT)
    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def setup_logger(name: str, log_path: str) -> logging.Logger:
    """Create a named logger with rotating file output and optional TTY console."""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=3
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout.isatty():
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        log.addHandler(ch)
    return log
