"""Metadata-only structured logging; evidence and secrets must not be logged."""

import json
import logging
from datetime import datetime, timezone
from collections.abc import Mapping
from pydantic import SecretStr


SENSITIVE_KEYS = ("secret", "token", "password", "authorization", "api_key", "credential", "cookie")


def redact(value):
    if isinstance(value, SecretStr):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if any(word in str(key).lower() for word in SENSITIVE_KEYS)
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class MetadataFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Do not serialize exception messages, arbitrary format arguments, HTTP bodies,
        # or evidence; they may contain credentials or student private information.
        return json.dumps({
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event_name", "application_event"),
            "metadata": redact(getattr(record, "metadata", {})),
        }, default=str)


def configure_logging() -> None:
    logger = logging.getLogger("supervisorai")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(MetadataFormatter())
        logger.addHandler(handler)
