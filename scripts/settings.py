# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Action inputs: parsing, validation and the build id field set."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from scripts import gha
from scripts.discovery import NAME_FROM_CHOICES, DiscoveryOptions, normalise_pattern
from scripts.gha import ActionError
from scripts.spec import ascii_lower

ORDER_CHOICES = ("declared", "dependencies")
MAX_SEARCH_DEPTH = 32
_LIST_SEPARATOR = re.compile(r"[,\s]+")
_PATH_SEPARATOR = re.compile(r"[,\n]+")


def _split(raw: str, separator: re.Pattern[str]) -> tuple[str, ...]:
    return tuple(item.strip() for item in separator.split(raw) if item.strip())


def _choice(value: str, label: str, choices: Sequence[str]) -> str:
    value = value.strip() or choices[0]
    if value not in choices:
        raise ActionError(f"{label} must be one of {', '.join(choices)}; got '{value}'")
    return value


def _depth(raw: str) -> int:
    raw = raw.strip() or "1"
    # ASCII digits only, and few of them: str.isdigit() also accepts
    # characters such as '²' that int() rejects, and int() refuses
    # strings past Python's digit limit (4,300 digits on 3.11+).
    if not re.fullmatch(r"[0-9]{1,4}", raw) or not 1 <= int(raw) <= MAX_SEARCH_DEPTH:
        raise ActionError(
            f"search_depth must be an integer from 1 to {MAX_SEARCH_DEPTH}; got '{raw}'"
        )
    return int(raw)


@dataclass(frozen=True)
class Settings:
    """The action inputs, parsed and validated."""

    path_prefix: str = "."
    images: str = ""
    repository: str = ""
    ref: str = ""
    gerrit_refspec: str = ""
    image_namespace: str = ""
    build_command: str = ""
    build_command_images: str = ""
    platforms: str = ""
    build_platform: str = ""
    discovery: DiscoveryOptions = field(default_factory=DiscoveryOptions)
    order: str = "declared"
    select: tuple[str, ...] = ()
    select_dependencies: bool = True
    allow_empty: bool = False
    summary: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        """Read the INPUT_* variables the composite action exports."""
        env = gha.env
        return cls(
            path_prefix=env("INPUT_PATH_PREFIX", "."),
            images=env("INPUT_IMAGES"),
            repository=env("INPUT_REPOSITORY"),
            ref=env("INPUT_REF"),
            gerrit_refspec=env("INPUT_GERRIT_REFSPEC"),
            image_namespace=env("INPUT_IMAGE_NAMESPACE"),
            build_command=env("INPUT_BUILD_COMMAND"),
            build_command_images=env("INPUT_BUILD_COMMAND_IMAGES"),
            platforms=env("INPUT_PLATFORMS"),
            build_platform=env("INPUT_BUILD_PLATFORM").strip(),
            discovery=DiscoveryOptions(
                search_depth=_depth(env("INPUT_SEARCH_DEPTH")),
                name_from=_choice(
                    env("INPUT_NAME_FROM"), "name_from", NAME_FROM_CHOICES
                ),
                exclude_paths=tuple(
                    normalise_pattern(p)
                    for p in _split(env("INPUT_EXCLUDE_PATHS"), _PATH_SEPARATOR)
                ),
            ),
            order=_choice(env("INPUT_ORDER"), "order", ORDER_CHOICES),
            select=tuple(
                ascii_lower(p) for p in _split(env("INPUT_SELECT"), _LIST_SEPARATOR)
            ),
            select_dependencies=gha.env_bool(
                "INPUT_SELECT_DEPENDENCIES", "select_dependencies", True
            ),
            allow_empty=gha.env_bool("INPUT_ALLOW_EMPTY", "allow_empty", False),
            summary=gha.env_bool("INPUT_SUMMARY", "summary", True),
        )

    @property
    def reads_dockerfiles(self) -> bool:
        """Whether resolving needs the same-repository dependency graph."""
        return self.order == "dependencies" or (
            bool(self.select) and self.select_dependencies
        )

    def ignored_with_explicit_images(self) -> list[str]:
        """Discovery-only tunables set away from their defaults."""
        defaults = DiscoveryOptions()
        return [
            name
            for name, active in (
                ("search_depth", self.discovery.search_depth != defaults.search_depth),
                ("name_from", self.discovery.name_from != defaults.name_from),
                ("exclude_paths", bool(self.discovery.exclude_paths)),
            )
            if active
        ]

    def id_fields(self) -> list[str]:
        """Inputs deciding what gets built, for the build id hash.

        The first nine are those the lanes hash, in their order, so the
        stem is unchanged for an unchanged configuration. A tunable is
        appended only when it differs from its default, for the same
        reason.
        """
        fields = [
            self.repository,
            self.ref,
            self.gerrit_refspec,
            self.path_prefix,
            self.images,
            self.image_namespace,
            self.build_command,
            self.build_command_images,
            self.platforms,
        ]
        defaults = Settings()
        tunables: list[tuple[str, object, object]] = [
            (
                "search_depth",
                self.discovery.search_depth,
                defaults.discovery.search_depth,
            ),
            ("name_from", self.discovery.name_from, defaults.discovery.name_from),
            ("exclude_paths", ",".join(self.discovery.exclude_paths), ""),
            ("order", self.order, defaults.order),
            ("select", ",".join(self.select), ""),
            (
                "select_dependencies",
                self.select_dependencies,
                defaults.select_dependencies,
            ),
            ("build_platform", self.build_platform, ""),
        ]
        fields.extend(
            f"{key}={value}" for key, value, default in tunables if value != default
        )
        return fields
