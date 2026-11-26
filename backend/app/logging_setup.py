"""Backend logging: console + rotating file data/logs/backend.log, every line tagged with the request id."""
from __future__ import annotations

import contextvars
import logging
import logging.handlers
import re
from pathlib import Path

request_id_var: contextvars.ContextVar = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


_SECRET_IN_URL = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)


class RedactTokens(logging.Filter):
    """The dashboard's WebSocket carries its token in the query string (/ws?token=...); uvicorn's access log would
    otherwise write it to the console in clear text. This rewrites it to token=*** before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def redact(text: str) -> str:
    return _SECRET_IN_URL.sub(r"\1***", text) if "token=" in text.lower() else text


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
    for name in ("aeisp", "uvicorn.access", "uvicorn.error", "uvicorn"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, RedactTokens) for f in lg.filters):
            lg.addFilter(RedactTokens())
