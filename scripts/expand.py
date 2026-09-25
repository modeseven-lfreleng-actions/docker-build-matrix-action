# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Variable expansion as BuildKit's shell lexer performs it.

Supports the forms BuildKit accepts (frontend/dockerfile/shell/lex.go):
``$NAME``, ``${NAME}``, ``${NAME-word}``, ``${NAME+word}``,
``${NAME?word}`` and their ``:`` variants, ``${NAME#pattern}``,
``${NAME##pattern}``, ``${NAME%pattern}``, ``${NAME%%pattern}``,
``${NAME/pattern/replacement}`` and ``${NAME//pattern/replacement}``.
Words may nest further expansions.

Anything it cannot evaluate makes the whole value unresolved, rather
than leaving an expression in place to be read as an image name. An
unset variable expands to an empty string, as in BuildKit, except
under ``?``, which BuildKit fails on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# BuildKit's isSpecialParam: each stands alone as a one-character name.
_SPECIAL = frozenset("@*#?-$!0")


def _is_digit(char: str) -> bool:
    # Go's unicode.IsDigit: Unicode Decimal_Number, which isdecimal() is.
    return char.isdecimal()


def _is_name_char(char: str) -> bool:
    # Go's unicode.IsLetter is the Letter categories, which isalpha() is.
    return char == "_" or char.isdecimal() or char.isalpha()


def _read_name(source: str, start: int) -> int:
    """End of the parameter name at ``start``, as BuildKit's processName.

    A digit run (``$1``, ``${12}``), a single special parameter, or
    Unicode letters, digits and ``_``. Returns ``start`` when none.
    """
    if start >= len(source):
        return start
    end = start
    if _is_digit(source[start]):
        while end < len(source) and _is_digit(source[end]):
            end += 1
        return end
    if source[start] in _SPECIAL:
        return start + 1
    while end < len(source) and _is_name_char(source[end]):
        end += 1
    return end


class _Unresolvable(Exception):
    """Raised when a value cannot be known before the build."""


def expand(value: str, variables: Mapping[str, str], escape: str = "\\") -> str | None:
    """Expand ``value``, or return None if any part is unresolvable.

    ``escape`` is the Dockerfile's escape token (``\\`` or a backtick),
    which BuildKit's lexer uses for every escape outside patterns.
    """
    try:
        return _Scanner(value, variables, escape=escape).text()
    except _Unresolvable:
        return None


def _pattern_regex(pattern: str, greedy: bool) -> str:
    """A shell pattern as BuildKit's convertShellPatternToRegex reads it.

    Only '*' and '?' are wildcards: brackets and every other character
    are literal, unlike fnmatch. '\\*', '\\?' and '\\\\' escape; an
    escape before '}' or '/' is dropped; any other escape is invalid.
    """
    out: list[str] = []
    position = 0
    while position < len(pattern):
        char = pattern[position]
        if char == "*":
            out.append(".*" if greedy else ".*?")
        elif char == "?":
            out.append(".")
        elif char == "\\":
            following = pattern[position + 1 : position + 2]
            if following in ("}", "/"):
                position += 1
                continue
            if following not in ("*", "?", "\\"):
                raise _Unresolvable(f"invalid escape in pattern '{pattern}'")
            out.append(re.escape(following))
            position += 1
        else:
            out.append(re.escape(char))
        position += 1
    return "".join(out)


def _reverse_pattern(pattern: str) -> str:
    """Reverse a pattern, keeping each escape before what it escapes."""
    tokens: list[str] = []
    position = 0
    while position < len(pattern):
        step = 2 if pattern[position] == "\\" and position + 1 < len(pattern) else 1
        tokens.append(pattern[position : position + step])
        position += step
    return "".join(reversed(tokens))


def _trim_prefix(value: str, pattern: str, greedy: bool) -> str:
    match = re.match(_pattern_regex(pattern, greedy), value)
    return value[match.end() :] if match else value


def _trim(value: str, pattern: str, prefix: bool, longest: bool) -> str:
    """``#``/``##`` (prefix) or ``%``/``%%`` (suffix) pattern removal.

    Suffixes use BuildKit's own approach: reverse the value and the
    pattern, and trim a prefix, since a regex cannot find the shortest
    rightmost match directly.
    """
    if prefix:
        return _trim_prefix(value, pattern, longest)
    return _trim_prefix(value[::-1], _reverse_pattern(pattern), longest)[::-1]


def _template_name(template: str, start: int) -> tuple[str, int] | None:
    """The ``$name``/``${name}`` at ``start`` (just past the '$').

    Go's Regexp.Expand: a name is letters, digits and '_', as long as
    possible in the bare form. Returns None when no valid name starts
    there, in which case Go writes the '$' literally.
    """
    if template.startswith("{", start):
        end = template.find("}", start + 1)
        name = template[start + 1 : end] if end >= 0 else ""
        if not name or not all(_is_template_char(c) for c in name):
            return None
        return name, end + 1
    end = start
    while end < len(template) and _is_template_char(template[end]):
        end += 1
    return (template[start:end], end) if end > start else None


def _is_template_char(char: str) -> bool:
    return char == "_" or char.isalpha() or char.isdecimal()


def _go_template(template: str, whole: str) -> str:
    """Expand a Go replacement template against one match.

    The pattern regex has no capture groups, so only group 0, the whole
    match, exists: '$0' and '${0}' give it, and every other name ('$1',
    '$00', '$0x' or a word) is empty. '$$' is a literal '$'.
    """
    out: list[str] = []
    position = 0
    while position < len(template):
        char = template[position]
        if char != "$":
            out.append(char)
            position += 1
        elif template.startswith("$$", position):
            out.append("$")
            position += 2
        elif (found := _template_name(template, position + 1)) is None:
            out.append("$")
            position += 1
        else:
            name, position = found
            out.append(whole if name == "0" else "")
    return "".join(out)


def _replace(value: str, pattern: str, replacement: str, every: bool) -> str:
    regex = re.compile(_pattern_regex(pattern, greedy=True))
    if not every:
        # BuildKit splices the replacement in literally here.
        return regex.sub(lambda _: replacement, value, count=1)
    # Go's ReplaceAllString, which BuildKit uses for '//', expands the
    # replacement as a template and ignores an empty match abutting the
    # previous match. Python's re.sub does neither, so ${B//*/x} on
    # 'abc' gave 'xx' where BuildKit gives 'x'.
    out: list[str] = []
    last = 0
    previous_end = -1
    for match in regex.finditer(value):
        if match.start() == match.end() == previous_end:
            continue
        out += [value[last : match.start()], _go_template(replacement, match.group())]
        last = previous_end = match.end()
    out.append(value[last:])
    return "".join(out)


class _Scanner:
    def __init__(
        self,
        source: str,
        variables: Mapping[str, str],
        raw_escapes: bool = False,
        escape: str = "\\",
    ) -> None:
        self.source = source
        self.variables = variables
        # Pattern words keep their escapes for the pattern converter.
        self.raw_escapes = raw_escapes
        self.escape = escape
        self.pos = 0

    def _escaped(self) -> str:
        """Consume the escape token and the character it escapes."""
        escaped = self.source[self.pos + 1]
        self.pos += 2
        return self.escape + escaped if self.raw_escapes else escaped

    def text(self) -> str:
        out: list[str] = []
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == self.escape:
                if self.pos + 1 >= len(self.source):
                    # BuildKit drops an escape token at the end of a word.
                    self.pos += 1
                    continue
                out.append(self._escaped())
            elif char == "$":
                out.append(self._dollar())
            elif char == "'":
                out.append(self._single_quoted())
            elif char == '"':
                out.append(self._double_quoted())
            else:
                out.append(char)
                self.pos += 1
        return "".join(out)

    def _single_quoted(self) -> str:
        """'...' is literal, quotes removed, as in BuildKit."""
        end = self.source.find("'", self.pos + 1)
        if end < 0:
            raise _Unresolvable("unterminated quote")
        literal = self.source[self.pos + 1 : end]
        self.pos = end + 1
        return literal

    def _double_quoted(self) -> str:
        """\"...\" expands variables, as BuildKit's processDoubleQuote.

        The escape token escapes only '\"', '$' and itself; before any
        other character it is literal. Backticks are not special.
        """
        out: list[str] = []
        self.pos += 1
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == '"':
                self.pos += 1
                return "".join(out)
            following = self.source[self.pos + 1 : self.pos + 2]
            if char == self.escape and following in ('"', "$", self.escape):
                out.append(self._escaped())
            elif char == "$":
                out.append(self._dollar())
            else:
                out.append(char)
                self.pos += 1
        raise _Unresolvable("unterminated quote")

    def _skip_quoted(self) -> None:
        """Move past a quoted run, so its contents cannot end a word."""
        quote = self.source[self.pos]
        self.pos += 1
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == self.escape and quote == '"':
                self.pos += 2
                continue
            self.pos += 1
            if char == quote:
                return
        raise _Unresolvable("unterminated quote")

    def _lookup(self, name: str) -> str:
        # BuildKit expands an unset variable to an empty string.
        return self.variables.get(name, "")

    def _raw_until(self, stops: str) -> str:
        """Raw text up to a stop character outside any nested ``${...}``
        or quotes; the stop is consumed."""
        start, depth = self.pos, 0
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == self.escape:
                self.pos += 2
                continue
            if char in "'\"":
                self._skip_quoted()
                continue
            if self.source.startswith("${", self.pos):
                depth += 1
                self.pos += 2
                continue
            if char == "}" and depth:
                depth -= 1
            elif char in stops and not depth:
                self.pos += 1
                return self.source[start : self.pos - 1]
            elif char == "}":
                # The expression closed before the stop, e.g. ${A/x}.
                raise _Unresolvable("bad substitution")
            self.pos += 1
        raise _Unresolvable("missing '}'")

    def _word(self, raw: str, raw_escapes: bool = False) -> str:
        return _Scanner(raw, self.variables, raw_escapes, self.escape).text()

    def _dollar(self) -> str:
        self.pos += 1
        if not self.source.startswith("{", self.pos):
            end = _read_name(self.source, self.pos)
            if end == self.pos:
                return "$"
            name, self.pos = self.source[self.pos : end], end
            return self._lookup(name)
        self.pos += 1
        end = _read_name(self.source, self.pos)
        if end == self.pos or self.source[self.pos] in "{}:":
            raise _Unresolvable("bad substitution")
        name, self.pos = self.source[self.pos : end], end
        if self.pos >= len(self.source):
            raise _Unresolvable("missing '}'")
        op = self.source[self.pos]
        self.pos += 1
        if op == "}":
            return self._lookup(name)
        if op == "/":
            return self._substitute(name)
        null_is_unset = op == ":"
        if null_is_unset:
            op = self.source[self.pos] if self.pos < len(self.source) else ""
            self.pos += 1
            if not op or op not in "-+?":
                raise _Unresolvable("unsupported modifier")
        if op not in "-+?#%":
            raise _Unresolvable("unsupported modifier")
        return self._modify(name, op, null_is_unset, self._raw_until("}"))

    def _modify(self, name: str, op: str, null_is_unset: bool, raw: str) -> str:
        value = self.variables.get(name)
        present = value is not None and not (null_is_unset and value == "")
        if op == "-":
            return value if present and value is not None else self._word(raw)
        if op == "+":
            return self._word(raw) if present else ""
        if op == "?":
            if not present or value is None:
                raise _Unresolvable(f"{name} must be set")
            return value
        # '#'/'##' trim a prefix, '%'/'%%' a suffix; doubled is greedy.
        longest = raw.startswith(op)
        pattern = self._word(raw[1:] if longest else raw, raw_escapes=True)
        return _trim(self._lookup(name), pattern, prefix=op == "#", longest=longest)

    def _substitute(self, name: str) -> str:
        every = self.source.startswith("/", self.pos)
        if every:
            self.pos += 1
        pattern = self._word(self._raw_until("/"), raw_escapes=True)
        replacement = self._word(self._raw_until("}"), raw_escapes=True)
        return _replace(self._lookup(name), pattern, replacement, every)
