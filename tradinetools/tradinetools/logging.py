"""Logging setup shared across all tradinebotte services."""

import logging
import logging.handlers
import sys


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


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
