"""Backend logging: console + rotating file data/logs/backend.log, every line tagged with the request id."""
from __future__ import annotations

import contextvars
import logging
import logging.handlers
from pathlib import Path

request_id_var: contextvars.ContextVar = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s"


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """Idempotent: calling it again (for example in tests) does not add duplicate handlers."""
    logger = logging.getLogger("aeisp")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    for h in list(logger.handlers):  # remove what an earlier call installed
        if getattr(h, "_aeisp", False):
            logger.removeHandler(h)
            h.close()
    fmt = logging.Formatter(_FORMAT)
    flt = _RequestIdFilter()
    fh = logging.handlers.RotatingFileHandler(
        str(log_dir / "backend.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ch = logging.StreamHandler()
    for h in (fh, ch):
        h._aeisp = True  # type: ignore[attr-defined]
        h.setFormatter(fmt)
        h.addFilter(flt)
        logger.addHandler(h)