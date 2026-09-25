# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Same-repository dependency graph and build ordering.

A reference names a sibling only when it could resolve to an image
this build produces, as the lanes tag them: unqualified (``base:verify``,
which Docker also spells ``docker.io/library/base``), or under the
configured image namespace on any registry
(``nexus3.onap.org:10001/onap/base:1.2`` with namespace ``onap``).
A third party's image that happens to share a sibling's final name,
such as ``ghcr.io/vendor/app``, is not one: a false edge is not
harmless, since with a real edge the other way it makes a cycle and
fails the run.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence

from scripts.gha import ActionError

# Docker's reference normalisation aliases these, and only these, to
# Docker Hub; registry-1.docker.io is Hub's endpoint but not an alias,
# and docker build does not resolve it to a local tag (verified).
_DOCKER_HUB = frozenset({"docker.io", "index.docker.io"})


def split_reference(reference: str) -> tuple[str, str]:
    """(registry, repository path) with tag and digest removed.

    The first component is a registry when Docker's reference grammar
    says so: it holds a '.' or ':', or is 'localhost'. Docker Hub's
    names normalise away, so 'docker.io/library/x' is plain 'x'.
    """
    reference = reference.split("@", 1)[0]
    if reference.rfind(":") > reference.rfind("/"):
        reference = reference[: reference.rfind(":")]
    reference = reference.lower()
    first, sep, rest = reference.partition("/")
    registry = ""
    if sep and ("." in first or ":" in first or first == "localhost"):
        registry, reference = first, rest
    if registry in _DOCKER_HUB:
        registry = ""
        if reference.startswith("library/") and reference.count("/") == 1:
            reference = reference[len("library/") :]
    return registry, reference


def sibling(reference: str, index: dict[str, int], namespace: str) -> int | None:
    """The sibling ``reference`` resolves to, if it names one.

    A plain namespace ('onap') matches on any registry, since the
    lanes push one tag set to several. A namespace whose first part is
    itself a registry ('ghcr.io/org') is split by the same rule as a
    reference, and then the registry must match too.
    """
    registry, path = split_reference(reference)
    if not registry and path in index:
        return index[path]
    if not namespace:
        return None
    ns_registry, ns_path = split_reference(f"{namespace}/_")
    ns_path = ns_path.removesuffix("_")
    if ns_registry and registry != ns_registry:
        return None
    if path.startswith(ns_path) and path[len(ns_path) :] in index:
        return index[path[len(ns_path) :]]
    return None


def local_dependencies(
    names: Sequence[str], references: Sequence[Sequence[str]], namespace: str = ""
) -> list[list[int]]:
    """For each image, the indices of the siblings it builds from."""
    index = {name: position for position, name in enumerate(names)}
    graph: list[list[int]] = []
    for position, refs in enumerate(references):
        deps = {
            found
            for ref in refs
            if (found := sibling(ref, index, namespace)) is not None
        }
        deps.discard(position)
        graph.append(sorted(deps))
    return graph


def topological_order(names: Sequence[str], deps: Sequence[Sequence[int]]) -> list[int]:
    """Kahn's algorithm, always taking the earliest-declared ready image.

    The result is the declared order wherever the dependencies allow
    it, so enabling ordering on an already-correct list changes
    nothing.
    """
    pending = [len(d) for d in deps]
    dependants: list[list[int]] = [[] for _ in names]
    for child, parents in enumerate(deps):
        for parent in parents:
            dependants[parent].append(child)
    ready = [position for position, count in enumerate(pending) if count == 0]
    heapq.heapify(ready)
    order: list[int] = []
    while ready:
        current = heapq.heappop(ready)
        order.append(current)
        for child in dependants[current]:
            pending[child] -= 1
            if pending[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(names):
        stuck = [position for position, count in enumerate(pending) if count > 0]
        raise ActionError(_cycle_message(names, deps, stuck))
    return order


def _reachable(start: int, deps: Sequence[Sequence[int]], within: set[int]) -> set[int]:
    seen: set[int] = set()
    stack = [parent for parent in deps[start] if parent in within]
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(parent for parent in deps[node] if parent in within)
    return seen


def _cycle_message(
    names: Sequence[str], deps: Sequence[Sequence[int]], stuck: list[int]
) -> str:
    """Name each cycle's members, and separately what they block.

    The images left unordered include those merely downstream of a
    cycle. A cycle is a set of mutually reachable images; an image on
    no such set is reported as blocked rather than as a member.
    """
    within = set(stuck)
    reach = {node: _reachable(node, deps, within) for node in stuck}
    cycles: list[list[str]] = []
    blocked: list[str] = []
    placed: set[int] = set()
    for node in stuck:
        if node in placed:
            continue
        if node not in reach[node]:
            blocked.append(names[node])
            continue
        members = {node} | {other for other in reach[node] if node in reach[other]}
        placed |= members
        cycles.append(sorted(names[member] for member in members))
    message = "Image dependency cycle between: " + "; ".join(
        ", ".join(cycle) for cycle in sorted(cycles)
    )
    if blocked:
        message += f" (also blocked: {', '.join(sorted(blocked))})"
    return message


def build_levels(
    order: Sequence[int], deps: Sequence[Sequence[int]]
) -> list[list[int]]:
    """Group an ordering into stages whose members are independent.

    Every image in stage N depends only on images in earlier stages,
    so each stage can build in parallel once its predecessors finish.
    """
    level: dict[int, int] = {}
    for position in order:
        level[position] = 1 + max(
            (level[parent] for parent in deps[position]), default=-1
        )
    stages: list[list[int]] = [[] for _ in range(max(level.values(), default=-1) + 1)]
    for position in order:
        stages[level[position]].append(position)
    return stages


def with_dependencies(selected: set[int], deps: Sequence[Sequence[int]]) -> set[int]:
    """Extend a selection with every sibling it transitively builds from."""
    closure = set(selected)
    stack = list(selected)
    while stack:
        for parent in deps[stack.pop()]:
            if parent not in closure:
                closure.add(parent)
                stack.append(parent)
    return closure
