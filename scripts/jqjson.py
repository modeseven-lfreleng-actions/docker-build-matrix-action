# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""JSON read and written exactly as ``jq -c`` does.

images_json is the lanes' contract, and they produced it with jq, so
the bytes must match for any valid input, not only the common case.

jq (1.7 and later) keeps each number's literal, rendered in canonical
decimal form: ``1e-7`` becomes ``1E-7``, ``1e2`` becomes ``1E+2``,
``1.50`` stays ``1.50``, ``-0`` stays ``-0`` and large integers stay
exact. That form is the General Decimal Arithmetic to-scientific-string
conversion, which ``decimal.Decimal.__str__`` implements, so numbers
are parsed as Decimal. Python's float rendering would turn ``1e-7``
into ``1e-07`` and lose digits.

Strings match Python's escaping except for DEL, which jq writes as
``\\u007f``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def _reject_constant(name: str) -> object:
    # NaN and Infinity are not JSON. jq coerces NaN to null; carried
    # through, they would make the outputs unparsable, so they fail.
    raise ValueError(f"{name} is not valid JSON")


def loads(text: str) -> Any:
    """Parse JSON, keeping every number's literal value."""
    return json.loads(
        text, parse_float=Decimal, parse_int=Decimal, parse_constant=_reject_constant
    )


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError(f"{value} is not valid JSON")
    return str(value)


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _object(value: dict[Any, Any]) -> str:
    return (
        "{" + ",".join(f"{_string(str(k))}:{dumps(v)}" for k, v in value.items()) + "}"
    )


def _array(value: list[Any] | tuple[Any, ...]) -> str:
    return "[" + ",".join(dumps(item) for item in value) + "]"


# bool precedes int, which it subclasses; lookup walks the MRO.
_WRITERS: dict[type, Any] = {
    type(None): lambda _: "null",
    bool: lambda value: "true" if value else "false",
    Decimal: _decimal,
    int: str,
    str: _string,
    dict: _object,
    list: _array,
    tuple: _array,
}


def dumps(value: Any) -> str:
    """Compact JSON, byte-compatible with ``jq -c``."""
    for kind in type(value).__mro__:
        writer = _WRITERS.get(kind)
        if writer is not None:
            return writer(value)
    raise TypeError(f"cannot serialise {type(value).__name__} as JSON")
