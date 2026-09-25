# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The shapes the resolved image set is published in."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any

from scripts import jqjson
from scripts.gha import markdown_cell
from scripts.settings import Settings
from scripts.spec import Image, build_args_of, dockerfile_of, platforms_of, target_of


@dataclass
class Resolution:
    """The ordered image set and everything derived from it."""

    images: list[Image]
    source: str
    levels: list[list[str]]
    depends_on: list[list[str]]
    build_id: str
    analysed: bool = False
    messages: list[tuple[str, str]] = field(default_factory=list)


def compact(value: object) -> str:
    """Single-line JSON, byte for byte as ``jq -c`` writes it."""
    return jqjson.dumps(value)


def matrix_entries(settings: Settings, result: Resolution) -> list[dict[str, Any]]:
    """One strategy.matrix entry per image, fields normalised.

    Unknown keys in an explicit entry pass through, so callers can
    attach their own per-image settings for matrix jobs to read.
    """
    prefix = settings.path_prefix or "."
    level_of = {name: n for n, stage in enumerate(result.levels) for name in stage}
    entries = []
    for index, (image, depends_on) in enumerate(
        zip(result.images, result.depends_on, strict=True)
    ):
        name = image["name"]
        dockerfile = dockerfile_of(image)
        entry = dict(image)
        entry.update(
            name=name,
            context=image["context"],
            dockerfile=dockerfile,
            target=target_of(image),
            build_args=build_args_of(image),
            platforms=platforms_of(image, settings.platforms),
            image=f"{settings.image_namespace}/{name}"
            if settings.image_namespace
            else name,
            context_path=posixpath.normpath(posixpath.join(prefix, image["context"])),
            dockerfile_path=posixpath.normpath(posixpath.join(prefix, dockerfile)),
            depends_on=depends_on,
            level=level_of[name],
            index=index,
        )
        entries.append(entry)
    return entries


def outputs(settings: Settings, result: Resolution) -> dict[str, str]:
    """Every step output, as the strings GITHUB_OUTPUT carries."""
    return {
        "images_json": compact(result.images),
        "image_count": str(len(result.images)),
        "image_names": " ".join(image["name"] for image in result.images),
        "matrix": compact({"include": matrix_entries(settings, result)}),
        "build_levels": compact(result.levels),
        "build_id": result.build_id,
        "source": result.source,
    }


def summary(result: Resolution) -> str:
    """The step summary table; a dependency column when it is known."""
    graph = result.analysed
    lines = ["## Docker Images", ""]
    if graph:
        lines += [
            "| Image | Dockerfile | Context | Builds from |",
            "| ----- | ---------- | ------- | ----------- |",
        ]
    else:
        lines += [
            "| Image | Dockerfile | Context |",
            "| ----- | ---------- | ------- |",
        ]
    for image, depends_on in zip(result.images, result.depends_on, strict=True):
        cells = [image["name"], dockerfile_of(image), image["context"]]
        if graph:
            cells.append(", ".join(depends_on) or "—")
        # Paths come from the caller or the repository tree, so each
        # cell is escaped: a '|' or line break would inject Markdown.
        lines.append("| " + " | ".join(markdown_cell(cell) for cell in cells) + " |")
    return "\n".join(lines) + "\n\n"


def log_lines(result: Resolution) -> list[str]:
    """The plain-text listing printed to the job log."""
    return [f"Discovered {len(result.images)} image(s):"] + [
        f"  {image['name']}  ({dockerfile_of(image)})" for image in result.images
    ]
