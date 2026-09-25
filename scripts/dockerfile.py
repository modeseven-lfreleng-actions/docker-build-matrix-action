# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Extract the image references a Dockerfile builds from.

Only what ordering needs is modelled: ``FROM`` bases, and ``COPY
--from`` and ``RUN --mount=...,from=`` sources, after substituting
global ``ARG`` values where BuildKit does. An unset
variable expands to an empty string, as in BuildKit. A reference that
cannot be evaluated, or that expands to nothing, is reported as
unresolved rather than guessed, because a wrong guess would reorder
the build.

Line handling follows BuildKit's parser (frontend/dockerfile/parser):
the ``escape`` directive, continuation joining, comment and blank line
skipping, and heredoc bodies. Where BuildKit's behaviour and a reading
of its source might differ, the tests pin what docker build does.
"""

from __future__ import annotations

import csv
import json
import re
import shlex
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from scripts.expand import expand
from scripts.platforms import automatic_arguments, targets
from scripts.platforms import resolve as resolve_platform

# Parser directives lead the file, before any comment, blank line or
# instruction; only 'escape' changes how the rest is read.
_DIRECTIVE = re.compile(r"#\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*(.*?)\s*")
# A heredoc opener word, quotes still in place: an optional file
# descriptor, '<<' or '<<-', then a delimiter with no '<' in it. The
# last rule is what keeps a shell here-string (<<<EOF) from opening one.
_HEREDOC = re.compile(r"(\d*)<<(-?)([^<]*)", re.DOTALL)
_BARE_OPENER = re.compile(r"\d*<<-?")
_INSTRUCTION = re.compile(r"([A-Za-z]+)(?:\s+(.*))?", re.DOTALL)
_HEREDOC_KEYWORDS = frozenset({"RUN", "COPY", "ADD"})


@dataclass(frozen=True)
class _Heredoc:
    delimiter: str
    strip_tabs: bool

    def ends_at(self, line: str) -> bool:
        """Terminators match exactly; only ``<<-`` strips leading tabs."""
        line = line.rstrip("\r\n")
        return (line.lstrip("\t") if self.strip_tabs else line) == self.delimiter


def _words(arguments: str) -> list[str]:
    try:
        return shlex.split(arguments)
    except ValueError:
        return arguments.split()


def _raw_words(line: str, escape: str = "\\") -> list[str]:
    """Split on unquoted whitespace, keeping quotes and escapes in place.

    A port of BuildKit's parseWords: the escape token keeps the next
    character with it (except inside single quotes, where it is
    literal), and one at the very end of the line is dropped.
    """
    words: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for position, char in enumerate(line):
        if escaped:
            current.append(char)
            escaped = False
        elif char == escape and quote != "'":
            if position == len(line) - 1:
                break
            current.append(char)
            escaped = True
        elif quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif char in "'\"":
            current.append(char)
            quote = char
        elif char.isspace():
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        words.append("".join(current))
    return words


def _heredocs(arguments: str) -> list[_Heredoc]:
    """Heredocs a RUN, COPY or ADD argument string opens, in order."""
    words = _raw_words(arguments)
    found = []
    for position, word in enumerate(words):
        if not word.startswith(("<", *"0123456789")):
            continue
        bare = _BARE_OPENER.fullmatch(word)
        if bare:
            # docker build accepts a space after the operator (RUN << EOF).
            # The operator word alone decides '-': in RUN << -EOF the
            # delimiter is '-EOF', with no tab stripping.
            if position + 1 >= len(words):
                continue
            strip_tabs, rest = word.endswith("-"), words[position + 1]
            if "<" in rest:
                continue
        else:
            match = _HEREDOC.fullmatch(word)
            if not match or not match.group(3):
                continue
            strip_tabs, rest = match.group(2) == "-", match.group(3)
        delimiter = _words(rest)
        if len(delimiter) == 1:
            found.append(_Heredoc(delimiter[0], strip_tabs))
    return found


def _is_json_form(arguments: str) -> bool:
    if not arguments.startswith("["):
        return False
    try:
        value = json.loads(arguments)
    except ValueError:
        return False
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _opened_heredocs(keyword: str, arguments: str) -> list[_Heredoc]:
    """Heredocs an instruction opens, looking through ONBUILD."""
    if keyword == "ONBUILD":
        match = _INSTRUCTION.fullmatch(arguments)
        if not match:
            return []
        keyword, arguments = match.group(1).upper(), (match.group(2) or "").strip()
    if keyword not in _HEREDOC_KEYWORDS or "<<" not in arguments:
        return []
    if _is_json_form(arguments):
        return []
    return _heredocs(arguments)


def _escape_token(lines: list[str]) -> str:
    for line in lines:
        match = _DIRECTIVE.fullmatch(line.strip())
        if not match:
            break
        if match.group(1).lower() == "escape" and match.group(2) in ("\\", "`"):
            return match.group(2)
    return "\\"


@dataclass
class References:
    """What a Dockerfile pulls in, split by whether it resolved."""

    images: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def _instructions(lines: list[str], escape_token: str) -> Iterator[tuple[str, str]]:
    """Yield (KEYWORD, arguments) for each logical instruction.

    Joins continuations on the file's escape token, drops comment and
    blank lines (including inside a continuation, as BuildKit does) and
    skips heredoc bodies, whose lines could otherwise pass for
    instructions.
    """
    escape = re.escape(escape_token)
    continuation = re.compile(rf"(?:(?<=[^{escape}])|^){escape}[ \t]*$")
    pending: str | None = None
    heredocs: list[_Heredoc] = []
    for line in lines:
        if heredocs:
            if heredocs[0].ends_at(line):
                heredocs.pop(0)
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        piece = line.lstrip() if pending is None else line
        joined, continues = continuation.subn("", piece)
        pending = (pending or "") + joined
        if continues:
            continue
        parsed = _parse_logical(pending)
        pending = None
        if parsed:
            heredocs = _opened_heredocs(*parsed)
            yield parsed
    # docker build still parses a continuation left open at the end of
    # the file (verified), so the last instruction is not dropped.
    if pending is not None:
        parsed = _parse_logical(pending)
        if parsed:
            yield parsed


def _parse_logical(logical: str) -> tuple[str, str] | None:
    match = _INSTRUCTION.fullmatch(logical.strip())
    if not match:
        return None
    return match.group(1).upper(), (match.group(2) or "").strip()


def _arg_declarations(arguments: str, escape: str) -> Iterator[tuple[str, str | None]]:
    # Words keep their quotes and escapes; expanding the default with
    # the file's escape token removes them, as BuildKit does.
    for token in _raw_words(arguments, escape):
        name, sep, default = token.partition("=")
        yield name, (default if sep else None)


def build_arg_values(build_args: list[str]) -> dict[str, str]:
    """``KEY=VALUE`` strings as a mapping; bare ``KEY`` is skipped."""
    values: dict[str, str] = {}
    for arg in build_args:
        key, sep, value = arg.partition("=")
        if sep:
            values[key] = value
    return values


@dataclass
class _Stage:
    """One build stage: the stages it uses and the images it pulls."""

    parents: list[int] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def _stages(
    text: str, build_args: Mapping[str, str], seed: Mapping[str, str]
) -> tuple[list[_Stage], dict[str, int]]:
    """Split a Dockerfile into stages, with names mapped to indices."""
    stages: list[_Stage] = []
    names: dict[str, int] = {}
    variables: dict[str, str] = dict(seed)
    # BuildKit's scanner splits physical lines on LF alone and trims
    # trailing CRs; splitlines() would also split on VT, FF, NEL and
    # U+2028, turning argument text into fake instructions.
    lines = [line.rstrip("\r") for line in text.removeprefix("\ufeff").split("\n")]
    escape = _escape_token(lines)

    def classify(
        raw: str, stage: _Stage, *, expands: bool = True, numeric_stage: bool = False
    ) -> None:
        """Record ``raw`` as an earlier stage, an image, or unresolved.

        FROM expands variables. COPY --from and RUN --mount from= do
        not: docker build rejects expansion there, so such a value is
        unresolved. A number names a stage only in COPY --from; in a
        mount, docker build pulls it as an image.
        """
        if expands:
            image = expand(raw, variables, escape)
        else:
            image = None if "$" in raw else raw
        # An empty result (FROM ${UNSET}) cannot be built, so it is
        # reported with the unevaluable ones rather than dropped.
        if not image:
            stage.unresolved.append(raw)
            return
        lowered = image.lower()
        if numeric_stage and image.isdigit():
            if int(image) < len(stages) - 1:
                stage.parents.append(int(image))
        elif lowered in names:
            stage.parents.append(names[lowered])
        elif lowered != "scratch":
            stage.images.append(image)

    for keyword, arguments in _instructions(lines, escape):
        if keyword == "ARG" and not stages:
            # Only global ARGs (before the first FROM) reach FROM lines.
            for name, default in _arg_declarations(arguments, escape):
                if name in build_args:
                    variables[name] = build_args[name]
                elif default is not None:
                    resolved = expand(default, variables, escape)
                    if resolved is not None:
                        variables[name] = resolved
        elif keyword == "FROM":
            tokens = arguments.split()
            while tokens and tokens[0].startswith("--"):
                tokens.pop(0)
            if not tokens:
                continue
            stage = _Stage()
            stages.append(stage)
            # FROM names only earlier stages; its own alias, registered
            # below, cannot refer to itself (FROM x AS x pulls image x).
            classify(tokens[0], stage)
            if len(tokens) >= 3 and tokens[1].lower() == "as":
                names[tokens[2].lower()] = len(stages) - 1
        elif keyword in {"COPY", "ADD"} and stages:
            for source in _flag_values(_leading_flags(arguments, escape), "from"):
                classify(source, stages[-1], expands=False, numeric_stage=True)
        elif keyword == "RUN" and stages:
            for mount in _flag_values(_leading_flags(arguments, escape), "mount"):
                for source in _mount_sources(mount):
                    classify(source, stages[-1], expands=False)
    return stages, names


def _leading_flags(arguments: str, escape: str) -> list[str]:
    """The ``--flag`` words before the first other word, unquoted.

    docker build reads flags only there: a later '--from=x' or
    '--mount=...' is an ordinary argument (verified: neither pulls x).
    """
    flags: list[str] = []
    for word in _raw_words(arguments, escape):
        if not word.startswith("--"):
            break
        flags.append("".join(_words(word)) or word)
    return flags


def _flag_values(flags: list[str], name: str) -> list[str]:
    prefix = f"--{name}="
    return [flag[len(prefix) :] for flag in flags if flag.startswith(prefix)]


def _mount_sources(value: str) -> list[str]:
    """The ``from=`` fields of a CSV ``--mount`` value."""
    try:
        fields = next(csv.reader([value]), [])
    except csv.Error:
        return []
    sources = []
    for item in fields:
        key, sep, source = item.partition("=")
        if sep and key.strip().lower() == "from":
            sources.append(source.strip())
    return sources


def references(
    text: str,
    build_args: Mapping[str, str],
    target: str = "",
    platforms: str = "",
    build_platform: str = "",
) -> References:
    """Images the build of ``target`` pulls in, excluding its own stages.

    As in BuildKit, only stages the target (or, without one, the final
    stage) depends on are built, so references in other stages cannot
    order the build. An unknown target leaves every stage in scope:
    the build will fail regardless, and hiding references would not
    help diagnose it.

    BuildKit's automatic arguments are seeded for each target platform
    (the host's when ``platforms`` is empty), and the result is the
    union, since FROM base-${TARGETARCH} can need a different sibling
    per platform. A platform containerd cannot parse is reported as
    unresolved, since the build itself would fail on it.
    """
    found = References()
    stage = _target_stage(text, target)
    builder = resolve_platform(build_platform) if build_platform else None
    for spec in targets(platforms, builder):
        platform = resolve_platform(spec)
        if platform is None:
            found.unresolved.append(f"platform {spec}")
            continue
        automatic = automatic_arguments(platform, stage, builder)
        # docker build: an explicit build arg overrides an automatic
        # one even undeclared; a declared global default does too,
        # which _stages applies.
        seed = {**automatic, **{k: v for k, v in build_args.items() if k in automatic}}
        images, unresolved = _references_for(text, build_args, target, seed)
        found.images += [image for image in images if image not in found.images]
        found.unresolved += [raw for raw in unresolved if raw not in found.unresolved]
    return found


def _target_stage(text: str, target: str) -> str:
    """TARGETSTAGE: the target, else the final stage's name, else 'default'.

    Observed with docker build: a named final stage gives its name, an
    unnamed one 'default'. Stage names are literal, so a scan with no
    variables finds them.
    """
    if target:
        return target
    stages, names = _stages(text, {}, {})
    final = len(stages) - 1
    return next((name for name, index in names.items() if index == final), "default")


def _references_for(
    text: str, build_args: Mapping[str, str], target: str, seed: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    stages, names = _stages(text, build_args, seed)
    if target and target.lower() not in names:
        roots = list(range(len(stages)))
    elif target:
        roots = [names[target.lower()]]
    else:
        roots = [len(stages) - 1] if stages else []
    reachable: set[int] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current not in reachable:
            reachable.add(current)
            pending.extend(stages[current].parents)
    ordered = sorted(reachable)
    return (
        [item for position in ordered for item in stages[position].images],
        [item for position in ordered for item in stages[position].unresolved],
    )
