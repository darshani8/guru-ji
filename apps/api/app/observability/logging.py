"""Small structured logging helper with safe fields only."""

import logging
from ..policy.redaction import redact_mapping

def get_logger(name: str = "agentic-saffron") -> logging.Logger:
    return logging.getLogger(name)

def safe_event(logger: logging.Logger, event: str, **fields: object) -> None:
    logger.info("%s %s", event, redact_mapping(fields))

__all__ = ["get_logger", "safe_event"]
