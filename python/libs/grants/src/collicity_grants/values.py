"""Typed write values (technical design §5.2).

Text written to a spreadsheet or list is escaped so it can't become a formula:
``=WEBSERVICE(…)`` or ``=HYPERLINK(…)`` would send data out. Number fields accept only numbers.
"""

import math
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from collicity_grants.models import FieldSpec, FieldType

# Characters that make Excel, Sheets or SharePoint evaluate a cell, including the full-width
# forms some input methods produce (U+FF1D, U+FF0B, U+FF0D, U+FF20).
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20")


class InvalidValueError(ValueError):
    pass


def escape_formula(text: str) -> str:
    """Make ``text`` literal: a leading apostrophe stops the cell being a formula."""
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def as_number(value: Any) -> Decimal:
    """A finite number. Booleans and numeric strings are refused."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise InvalidValueError(f"expected a number, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidValueError("expected a finite number")
    try:
        number = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    except InvalidOperation as error:
        raise InvalidValueError("expected a finite number") from error
    if not number.is_finite():
        raise InvalidValueError("expected a finite number")
    return number


def typed_value(spec: FieldSpec, value: Any) -> Any:
    """The value to send for a field of type ``spec``, or InvalidValueError."""
    match spec.type:
        case FieldType.NUMBER:
            as_number(value)
            return value
        case FieldType.TEXT:
            if not isinstance(value, str):
                raise InvalidValueError(f"expected text, got {type(value).__name__}")
            return escape_formula(value)
        case FieldType.DATE:
            if not isinstance(value, str):
                raise InvalidValueError("expected an ISO date (YYYY-MM-DD)")
            try:
                date.fromisoformat(value)
            except ValueError as error:
                raise InvalidValueError("expected an ISO date (YYYY-MM-DD)") from error
            if len(value) != 10:
                raise InvalidValueError("expected an ISO date (YYYY-MM-DD)")
            return value
        case FieldType.CHOICE:
            if not isinstance(value, str) or value not in spec.choices:
                raise InvalidValueError(f"{value!r} isn't one of the field's choices")
            return value
