"""Persist chronological operational events and a separate owner audit trail."""

import logging
import re

from sqlalchemy.orm import Session

from .db import lock
from .models import AuditEvent, SystemEvent
from .serialization import jsonable

EVENT_LOCK = 18902461
logger = logging.getLogger("cauche.events")
REDACTED = "[REDACTED]"
SECRET_FIELDS = re.compile(
    r"secret|password|passwd|token|authorization|cookie|credential|private[_-]?key|"
    r"key[_-]?id|signature|otp|pem|certificate",
    re.IGNORECASE,
)
SECRET_TEXT = re.compile(
    r"-----BEGIN[^\n]*-----|\bbearer\s|"
    r"(?:secret|token|password|authorization|cookie|otp|signature)\s*[:=]",
    re.IGNORECASE,
)


def redact(value):
    if isinstance(value, str):
        return REDACTED if SECRET_TEXT.search(value) else value[:512]
    if isinstance(value, dict):
        return {
            str(key)[:64]: REDACTED if SECRET_FIELDS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value[:100]]
    return jsonable(value)


def event(session: Session, type: str, code: str, message: str, *, context=None, **scope) -> SystemEvent:
    # A sequence alone does not establish commit order across concurrent transactions.
    lock(session, EVENT_LOCK)
    row = SystemEvent(type=type, code=code, message=redact(message)[:255],
                      context=redact(context or {}), **scope)
    session.add(row)
    logger.info("event type=%s code=%s", type, code)
    return row


def audit(session: Session, type: str, code: str, message: str, *, user_id=None, context=None,
          severity: str = "INFO") -> None:
    row = event(session, type, code, message, context=context, severity=severity)
    session.add(AuditEvent(type=row.type, code=row.code, message=row.message, user_id=user_id,
                           context=row.context, severity=severity))
