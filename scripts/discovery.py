# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Dockerfile discovery under a project root.

The default walk (depth 1, names from directories) is the one the
docker-workflows lanes perform inline, in the same order:

1. ``Dockerfile`` at the root, named after the project
2. ``docker/Dockerfile``, named after the project
3. ``src/main/docker/Dockerfile`` (the Maven convention), likewise
4. ``<dir>/Dockerfile`` for each top-level directory in code point
   order, named after the directory, skipping ``docker`` and ``src``

A deeper walk extends step 4 to nested directories, visited in sorted
path order, for monorepos that group images (``services/api/``).
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from scripts.spec import Image

DOCKERFILE = "Dockerfile"

# (dockerfile, context) pairs named after the project, in walk order.
PROJECT_LOCATIONS = (
    ("Dockerfile", "."),
    ("docker/Dockerfile", "docker"),
    ("src/main/docker/Dockerfile", "src/main/docker"),
)
_PROJECT_DOCKERFILES = frozenset(dockerfile for dockerfile, _ in PROJECT_LOCATIONS)
PROJECT_CONTEXTS = frozenset(context for _, context in PROJECT_LOCATIONS)

# Top-level directories that hold project-named images (or, for src,
# a Maven tree) rather than being images themselves.
_RESERVED_TOP_LEVEL = frozenset({"docker", "src"})

NAME_FROM_CHOICES = ("directory", "path")


@dataclass(frozen=True)
class DiscoveryOptions:
    """How far to walk, how to name images, and what to skip."""

    search_depth: int = 1
    name_from: str = "directory"
    exclude_paths: tuple[str, ...] = ()


def coreutils_basename(path: str) -> str:
    """``basename(1)``: trailing slashes do not produce an empty name."""
    stripped = path.rstrip("/")
    if not stripped:
        return "/" if path else ""
    return stripped.rsplit("/", 1)[-1]


def project_name(repository: str, path_prefix: str) -> str:
    """Name for project-level images: the sub-project, else the repo.

    The prefix is normalised first, so equivalent spellings such as
    ``sub/``, ``sub/.`` and ``sub/child/..`` all name the project
    ``sub``, and ``./`` names it after the repository.
    """
    normalised = os.path.normpath(path_prefix or ".")
    if normalised != ".":
        return coreutils_basename(normalised)
    return repository.rsplit("/", 1)[-1]


def normalise_pattern(pattern: str) -> str:
    """Drop the ``./`` prefix and trailing ``/`` users tend to write."""
    while pattern.startswith("./"):
        pattern = pattern[2:]
    return pattern.rstrip("/") or "."


def is_excluded(relative: str, patterns: Sequence[str]) -> bool:
    """Whether a relative directory, or any directory above it, matches.

    Excluding a directory excludes everything below it, so ``src``
    also covers the fixed ``src/main/docker`` location.
    """
    if not patterns:
        return False
    parts = relative.split("/")
    ancestors = ["/".join(parts[: depth + 1]) for depth in range(len(parts))]
    return any(
        fnmatch.fnmatchcase(path, pattern) for path in ancestors for pattern in patterns
    )


def _directories(
    root: str, relative: str, level: int, options: DiscoveryOptions
) -> Iterator[str]:
    """Yield candidate image directories in sorted path order."""
    base = os.path.join(root, relative) if relative else root
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return
    for entry in entries:
        if entry.startswith("."):
            continue
        child = f"{relative}/{entry}" if relative else entry
        full = os.path.join(root, child)
        # isdir follows symbolic links, as the shell glob it replaces
        # did at the top level; descent below does not, so a link
        # cannot send the walk round a loop.
        if not os.path.isdir(full) or is_excluded(child, options.exclude_paths):
            continue
        if not (level == 1 and entry in _RESERVED_TOP_LEVEL):
            yield child
        if level < options.search_depth and not os.path.islink(full):
            yield from _directories(root, child, level + 1, options)


def _image(name: str, dockerfile: str, context: str) -> Image:
    # Key order matches the inline implementation's output byte for byte.
    return {"name": name, "dockerfile": dockerfile, "context": context}


def discover(root: str, default_name: str, options: DiscoveryOptions) -> list[Image]:
    """Walk ``root`` and return image entries in discovery order."""
    images: list[Image] = []
    for dockerfile, context in PROJECT_LOCATIONS:
        if os.path.isfile(os.path.join(root, dockerfile)) and not is_excluded(
            context, options.exclude_paths
        ):
            images.append(_image(default_name, dockerfile, context))
    for directory in _directories(root, "", 1, options):
        dockerfile = f"{directory}/{DOCKERFILE}"
        if dockerfile in _PROJECT_DOCKERFILES:
            continue
        if not os.path.isfile(os.path.join(root, dockerfile)):
            continue
        if options.name_from == "path":
            name = directory.replace("/", "-")
        else:
            name = directory.rsplit("/", 1)[-1]
        images.append(_image(name, dockerfile, directory))
    return images
