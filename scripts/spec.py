# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Image entry rules: the explicit input schema, naming and collisions.

These reproduce the inline ``jq`` implementation in docker-workflows
exactly, because the three lanes' build loops consume the result.
Two ``jq`` details matter and are easy to lose in translation:

* ``//`` substitutes its right-hand side only for ``null`` and
  ``false``, so an explicit ``"dockerfile": false`` is accepted as
  absent while ``"dockerfile": 0`` is rejected.
* ``ascii_downcase`` lowercases ASCII only. Python's ``str.lower``
  would expand some non-ASCII letters into two code points, and the
  replacement that follows would then emit two hyphens instead of one.
"""

from __future__ import annotations

import re
from typing import Any

from scripts import jqjson
from scripts.gha import ActionError

Image = dict[str, Any]

EXPLICIT_SCHEMA_ERROR = (
    "images input must be a non-empty JSON array of objects with string "
    "'name' and 'context' keys; optional 'dockerfile'/'target' take "
    "strings and 'build_args' a list of KEY=VALUE strings. Pass an empty "
    "string (not []) for auto-discovery"
)

# A Docker repository path component: starts and ends alphanumeric.
# This is the lanes' rule, kept so their messages stay identical.
_NAME = re.compile(r"[a-z0-9]([a-z0-9._-]*[a-z0-9])?")
_NOT_NAME_CHAR = re.compile(r"[^a-z0-9._-]")

# Docker's reference grammar for a path component, which the release
# lane applies to namespaces: alphanumeric runs joined by '.', '_',
# '__' or one or more '-'. Stricter than _NAME, which admits 'a..b'
# and 'a___b', names that docker tag rejects.
COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_COMPONENT = re.compile(COMPONENT)


def _absent(value: object) -> bool:
    """Mirror jq's ``//``, which falls through only on null and false."""
    return value is None or value is False


def _optional_string(entry: Image, key: str) -> bool:
    value = entry.get(key)
    return _absent(value) or isinstance(value, str)


def _valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    if not isinstance(entry.get("name"), str):
        return False
    if not isinstance(entry.get("context"), str):
        return False
    if not (
        _optional_string(entry, "dockerfile") and _optional_string(entry, "target")
    ):
        return False
    build_args = entry.get("build_args")
    if _absent(build_args):
        return True
    return isinstance(build_args, list) and all(
        isinstance(arg, str) for arg in build_args
    )


def parse_explicit(raw: str) -> list[Image]:
    """Parse and schema-check the caller's explicit ``images`` input."""
    try:
        data = jqjson.loads(raw)
        # The outputs re-serialise this, so it must survive as JSON
        # text; encoding refuses a lone surrogate ("\ud800"), which jq
        # rejects too.
        jqjson.dumps(data).encode("utf-8")
    except ValueError:
        data = None
    if not (
        isinstance(data, list) and data and all(_valid_entry(item) for item in data)
    ):
        raise ActionError(EXPLICIT_SCHEMA_ERROR)
    return data


def ascii_lower(text: str) -> str:
    """Lowercase A-Z only, as ``jq``'s ``ascii_downcase`` does."""
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text)


def sanitise_name(name: str) -> str:
    """Lowercase ASCII and replace characters Docker does not permit."""
    return _NOT_NAME_CHAR.sub("-", ascii_lower(name))


def sanitise_names(images: list[Image]) -> list[Image]:
    """Return copies with sanitised names; key order is preserved."""
    return [{**image, "name": sanitise_name(image["name"])} for image in images]


def duplicate_names(images: list[Image]) -> list[str]:
    """Names occurring more than once, sorted as ``jq group_by`` sorts."""
    seen: set[str] = set()
    dupes: set[str] = set()
    for image in images:
        name = image["name"]
        (dupes if name in seen else seen).add(name)
    return sorted(dupes)


def first_of_each_name(images: list[Image]) -> tuple[list[Image], list[Image]]:
    """Keep the first entry per name without reordering the rest.

    Order is build order, which same-repository ``FROM`` chains rely
    on, so this must not sort. Returns the kept and dropped entries.
    """
    kept: list[Image] = []
    dropped: list[Image] = []
    names: set[str] = set()
    for image in images:
        if image["name"] in names:
            dropped.append(image)
        else:
            names.add(image["name"])
            kept.append(image)
    return kept, dropped


def invalid_names(images: list[Image]) -> list[str]:
    """Names that are not valid Docker repository components."""
    return [image["name"] for image in images if not _NAME.fullmatch(image["name"])]


def untaggable_names(images: list[Image]) -> list[str]:
    """Names passing the lanes' check that Docker still rejects."""
    return [
        image["name"] for image in images if not _COMPONENT.fullmatch(image["name"])
    ]


def dockerfile_of(image: Image) -> str:
    """The Dockerfile path relative to the project root."""
    dockerfile = image.get("dockerfile")
    if _absent(dockerfile) or dockerfile == "":
        return f"{image['context']}/Dockerfile"
    return str(dockerfile)


def build_args_of(image: Image) -> list[str]:
    """The entry's build arguments, as a list."""
    build_args = image.get("build_args")
    return list(build_args) if isinstance(build_args, list) else []


def target_of(image: Image) -> str:
    """The entry's build target, or an empty string."""
    target = image.get("target")
    return "" if _absent(target) else str(target)


def _platform_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def invalid_platforms(images: list[Image]) -> list[str]:
    """Names whose ``platforms`` is neither a string nor a string list."""
    return [
        image["name"]
        for image in images
        if not (
            # Only a missing or null value is absent: unlike the lanes'
            # own keys, platforms has no jq '//' heritage, so false is
            # a wrong type like any other.
            image.get("platforms") is None
            or isinstance(image.get("platforms"), str)
            or _platform_list(image.get("platforms"))
        )
    ]


def platforms_of(image: Image, default: str) -> str:
    """The entry's platforms as a comma-separated string, else ``default``.

    A list joins with commas, the form buildx and build-push-action
    take. Components are trimmed and empty ones dropped first, so a
    value with nothing in it (" ,", ["", " "]) falls back to the
    action input rather than reaching the matrix.
    """
    value = image.get("platforms")
    if isinstance(value, list) and _platform_list(value):
        items = [str(item) for item in value]
    elif isinstance(value, str):
        items = [value]
    else:
        items = []
    return _clean_platforms(items) or _clean_platforms([default])


def _clean_platforms(items: list[str]) -> str:
    parts = [part.strip() for item in items for part in item.split(",")]
    return ",".join(part for part in parts if part)
