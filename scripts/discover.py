# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Resolve the image set, order it, and publish the outputs.

The composite action runs ``main`` through ``entrypoint.py``.

The checks up to and including name validation are, in order and in
wording, those of the inline implementation in the docker-workflows
lanes, and every default reproduces that implementation's result.
Selection and dependency ordering run after them and are opt-in.
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
import traceback

from scripts import gha, render
from scripts.buildid import build_id
from scripts.discovery import PROJECT_CONTEXTS, discover, project_name
from scripts.dockerfile import build_arg_values, references
from scripts.gha import ActionError
from scripts.graph import (
    build_levels,
    local_dependencies,
    topological_order,
    with_dependencies,
)
from scripts.platforms import resolve as resolve_platform
from scripts.render import Resolution
from scripts.settings import Settings
from scripts.spec import (
    COMPONENT,
    Image,
    build_args_of,
    dockerfile_of,
    duplicate_names,
    first_of_each_name,
    invalid_names,
    invalid_platforms,
    parse_explicit,
    platforms_of,
    sanitise_names,
    target_of,
    untaggable_names,
)

Messages = list[tuple[str, str]]
Graph = list[list[int]]

# Each '/'-separated namespace component follows the grammar image
# names do; see spec.COMPONENT.
_NAMESPACE = re.compile(rf"{COMPONENT}(?:/{COMPONENT})*")
# Docker's limit on a repository name (reference.NameTotalLengthMax).
MAX_REPOSITORY = 255


def _within(workspace: str, path: str) -> bool:
    """Whether ``path`` resolves, symbolic links included, inside it.

    A path that cannot be resolved at all, such as one carrying a NUL
    from an escaped JSON string, counts as outside.
    """
    try:
        base = os.path.realpath(workspace)
        return os.path.commonpath([base, os.path.realpath(path)]) == base
    except (OSError, ValueError):
        return False


def _root(settings: Settings, workspace: str) -> str:
    if settings.image_namespace and not _NAMESPACE.fullmatch(settings.image_namespace):
        raise ActionError(
            f"Invalid image_namespace '{settings.image_namespace}' "
            "(each '/'-separated component must be lowercase alphanumeric "
            "runs joined by '.', '_', '__' or '-')"
        )
    prefix = settings.path_prefix or "."
    root = os.path.join(workspace, prefix)
    if os.path.isabs(prefix) or not _within(workspace, root):
        raise ActionError(
            f"path_prefix '{settings.path_prefix}' must be a path inside the workspace"
        )
    if not os.path.isdir(root):
        raise ActionError(f"path_prefix '{settings.path_prefix}' is not a directory")
    return root


def _contain(workspace: str, root: str, images: list[Image]) -> None:
    """Refuse contexts or Dockerfiles resolving outside the workspace.

    The boundary is the workspace, not path_prefix: a context above the
    Dockerfile's directory, such as ../shared, is ordinary Docker usage,
    whereas a path out of the checkout would hand downstream builds and
    the Dockerfile reader files the repository does not own.
    """
    escapes = [
        f"{image['name']} ({field} '{path}')"
        for image in images
        for field, path in (
            ("context", image["context"]),
            ("dockerfile", dockerfile_of(image)),
        )
        if os.path.isabs(path) or not _within(workspace, os.path.join(root, path))
    ]
    if escapes:
        raise ActionError(
            f"Image paths must stay inside the workspace: {'; '.join(escapes)}"
        )


def _load(settings: Settings, root: str, messages: Messages) -> list[Image]:
    """The explicit list, or discovery's, with names normalised."""
    if settings.images:
        images = sanitise_names(parse_explicit(settings.images))
        ignored = settings.ignored_with_explicit_images()
        if ignored:
            messages.append(
                (
                    "notice",
                    f"Ignoring discovery-only input(s) with an explicit images input: {', '.join(ignored)}",
                )
            )
        dupes = duplicate_names(images)
        if dupes:
            raise ActionError(
                f"Duplicate image name(s) after normalisation in the images input: {', '.join(dupes)}"
            )
        return images
    found = sanitise_names(
        discover(
            root,
            project_name(settings.repository, settings.path_prefix),
            settings.discovery,
        )
    )
    images, dropped = first_of_each_name(found)
    if settings.discovery.name_from == "path":
        _refuse_path_collisions(images, dropped)
    for image in dropped:
        messages.append(
            (
                "log",
                f"Skipping {dockerfile_of(image)}: image name '{image['name']}' is already taken",
            )
        )
    return images


def _refuse_path_collisions(kept: list[Image], dropped: list[Image]) -> None:
    """In path mode, two directories sharing a name is an error.

    Path names promise one image per directory, but the mapping is not
    injective: a/b and a-b both become a-b, and sanitising merges more.
    Dropping one would build part of the tree without a word, so this
    fails instead. The project-level locations keep their legacy rule,
    where the first of root, docker/ and src/main/docker wins.
    """
    clashes = [image for image in dropped if image["context"] not in PROJECT_CONTEXTS]
    if not clashes:
        return
    first = {image["name"]: image["context"] for image in kept}
    detail = "; ".join(
        f"'{image['name']}' from {first[image['name']]} and {image['context']}"
        for image in clashes
    )
    raise ActionError(
        f"name_from: path gives two directories one image name: {detail}. "
        "Rename one, exclude it, or list the images explicitly"
    )


def _check(settings: Settings, images: list[Image], messages: Messages) -> None:
    """Refuse an empty or badly named set, unless emptiness is expected."""
    prefix = settings.path_prefix
    if not images:
        if settings.build_command:
            # Project tooling such as jib or a Gradle plugin synthesises
            # images with no Dockerfile to find; the caller's build job
            # enumerates whatever the command created.
            messages.append(
                (
                    "notice",
                    f"No Dockerfiles discovered under '{prefix}'; build_command builds the images",
                )
            )
        elif settings.allow_empty:
            messages.append(
                (
                    "notice",
                    f"No Dockerfiles found under '{prefix}'; allow_empty permits an empty result",
                )
            )
        else:
            raise ActionError(
                f"No Dockerfiles found under '{prefix}' and no images input provided"
            )
    bad = invalid_names(images)
    if bad:
        raise ActionError(
            f"Invalid image name(s) after sanitisation: {', '.join(bad)} (names must start/end with a-z or 0-9)"
        )
    # After the lanes' check, so each case they reject keeps its
    # message: the separator runs they admit still fail docker tag.
    untaggable = untaggable_names(images)
    if untaggable:
        raise ActionError(
            f"Invalid image name(s) after sanitisation: {', '.join(untaggable)} "
            "(separators must sit between alphanumerics, as one '.', "
            "one or two '_', or any run of '-')"
        )
    overlong = [
        image["name"]
        for image in images
        if len(_repository(settings, image)) > MAX_REPOSITORY
    ]
    if overlong:
        raise ActionError(
            f"Image repository name(s) longer than Docker's {MAX_REPOSITORY}-character "
            f"limit, namespace included: {', '.join(overlong)}"
        )
    bad_platforms = invalid_platforms(images)
    if bad_platforms:
        raise ActionError(
            "images entries whose 'platforms' is neither a string nor a list "
            f"of strings: {', '.join(bad_platforms)}"
        )
    unparsable = sorted(
        {
            spec
            for value in [settings.platforms] + [platforms_of(i, "") for i in images]
            for spec in (item.strip() for item in value.split(","))
            if spec and resolve_platform(spec) is None
        }
    )
    if unparsable:
        # buildx refuses these too; failing here names them up front.
        raise ActionError(
            f"Invalid platform(s): {', '.join(unparsable)} "
            "(expected os/arch[/variant], such as linux/amd64)"
        )
    # One builder, so one specifier: a list would parse as nothing and
    # silently fall back to the runner.
    builder = settings.build_platform
    if builder and ("," in builder or resolve_platform(builder) is None):
        raise ActionError(
            f"build_platform must be a single platform, such as linux/amd64; got '{builder}'"
        )


def _repository(settings: Settings, image: Image) -> str:
    name = image["name"]
    return f"{settings.image_namespace}/{name}" if settings.image_namespace else name


def _dependency_graph(
    root: str, images: list[Image], messages: Messages, settings: Settings
) -> Graph:
    """Which images build from which siblings, read from the Dockerfiles.

    ``platforms`` is the action-level default; each image's own
    platforms seed BuildKit's automatic TARGET* arguments.
    """
    refs: list[list[str]] = []
    for image in images:
        dockerfile = dockerfile_of(image)
        try:
            with open(
                os.path.join(root, dockerfile), encoding="utf-8", errors="replace"
            ) as handle:
                text = handle.read()
        except OSError as err:
            messages.append(
                (
                    "warning",
                    f"{image['name']}: cannot read {dockerfile} ({err.strerror}); "
                    "ordering assumes it builds from no sibling image",
                )
            )
            refs.append([])
            continue
        found = references(
            text,
            build_arg_values(build_args_of(image)),
            target_of(image),
            platforms_of(image, settings.platforms),
            settings.build_platform,
        )
        for raw in found.unresolved:
            messages.append(
                (
                    "warning",
                    f"{image['name']}: cannot resolve '{raw}' in {dockerfile}; "
                    "set a default or pass it in build_args to order by it",
                )
            )
        refs.append(found.images)
    return local_dependencies(
        [image["name"] for image in images], refs, settings.image_namespace
    )


def _select(
    settings: Settings, images: list[Image], deps: Graph | None, messages: Messages
) -> list[int]:
    """Indices of the selected images (and their bases), in order."""
    if not settings.select or not images:
        return list(range(len(images)))
    names = [image["name"] for image in images]
    chosen = {
        i
        for i, name in enumerate(names)
        if any(fnmatch.fnmatchcase(name, p) for p in settings.select)
    }
    for pattern in settings.select:
        if not any(fnmatch.fnmatchcase(name, pattern) for name in names):
            messages.append(("warning", f"select pattern '{pattern}' matched no image"))
    if deps is not None and settings.select_dependencies:
        # The graph also exists for order: dependencies, which must not
        # override an explicit select_dependencies: false.
        chosen = with_dependencies(chosen, deps)
    if not chosen:
        detail = f"select ({', '.join(settings.select)}) matched none of the {len(images)} image(s)"
        if not settings.allow_empty:
            raise ActionError(detail)
        messages.append(("notice", f"{detail}; allow_empty permits an empty result"))
    return sorted(chosen)


def _restrict(deps: Graph | None, indices: list[int]) -> Graph | None:
    """The graph over a subset, renumbered to the subset's positions."""
    if deps is None:
        return None
    position = {old: new for new, old in enumerate(indices)}
    return [[position[p] for p in deps[i] if p in position] for i in indices]


def resolve(settings: Settings, workspace: str = ".") -> Resolution:
    """Compute the ordered image set; raises ActionError on bad input."""
    messages: Messages = []
    root = _root(settings, workspace)
    images = _load(settings, root, messages)
    _check(settings, images, messages)
    _contain(workspace, root, images)

    deps = (
        _dependency_graph(root, images, messages, settings)
        if images and settings.reads_dockerfiles
        else None
    )
    indices = _select(settings, images, deps, messages)
    subset = [images[i] for i in indices]
    sub_deps = _restrict(deps, indices)
    names = [image["name"] for image in subset]

    if settings.order == "dependencies" and sub_deps is not None:
        order = topological_order(names, sub_deps)
        levels = build_levels(order, sub_deps)
    else:
        order = list(range(len(subset)))
        levels = [[k] for k in order]

    return Resolution(
        images=[subset[k] for k in order],
        source="explicit" if settings.images else "discovered",
        levels=[[names[k] for k in stage] for stage in levels],
        depends_on=[
            [names[p] for p in sub_deps[k]] if sub_deps is not None else []
            for k in order
        ],
        build_id=build_id(settings.id_fields()),
        analysed=sub_deps is not None,
        messages=messages,
    )


def _publish(settings: Settings, result: Resolution) -> None:
    for level, message in result.messages:
        if level == "log":
            gha.log(message)
        else:
            gha.annotate(level, message)
    for line in render.log_lines(result):
        gha.log(line)
    gha.set_outputs(render.outputs(settings, result))
    if settings.summary:
        gha.append_summary(render.summary(result))


def main() -> int:
    """Run the action; returns the process exit status."""
    try:
        settings = Settings.from_env()
        _publish(settings, resolve(settings))
    except ActionError as err:
        gha.annotate("error", str(err))
        return 1
    except Exception as err:
        # Every expected failure is an ActionError. Anything else is a
        # bug, but it must still fail the step with an annotation, not
        # a bare traceback the job summary never shows. Publication sits
        # inside the guard too: rendering and runner-file I/O can fail.
        traceback.print_exc()
        gha.annotate("error", f"Internal error in docker-build-matrix-action: {err!r}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
