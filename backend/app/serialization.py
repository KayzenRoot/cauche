"""Stable serialization of UTC timestamps and fixed-point values."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import inspect

from .db import aware


def jsonable(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return aware(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def record(row, *, exclude=()) -> dict:
    return {
        column.key: jsonable(getattr(row, column.key))
        for column in inspect(row).mapper.column_attrs
        if column.key not in exclude
    }
