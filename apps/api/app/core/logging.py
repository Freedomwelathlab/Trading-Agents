import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

_SECRET_KEY_PATTERN = re.compile(
    r"(secret|password|passwd|api[_-]?key|token|private[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"


def _redact_secrets(
    _logger: object, _method_name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """Strip anything that looks like a credential before it reaches a sink.

    Spec §38: never log API secrets, broker passwords, private keys, or
    auth tokens. This matches on key name, not value shape, so it is
    deliberately broad — over-redacting is the safe failure mode here.
    """
    for key in list(event_dict.keys()):
        if _SECRET_KEY_PATTERN.search(key):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(level=log_level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(log_level)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)
