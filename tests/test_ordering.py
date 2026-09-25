# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Dockerfile reference extraction and same-repository ordering."""

from __future__ import annotations

import unittest

from scripts.dockerfile import build_arg_values, references
from scripts.expand import expand
from scripts.gha import ActionError
from scripts.graph import (
    build_levels,
    local_dependencies,
    split_reference,
    topological_order,
    with_dependencies,
)


class ReferencesTest(unittest.TestCase):
    """What a Dockerfile builds from."""

    def refs(self, text: str, **build_args: str) -> list[str]:
        return references(text, build_args).images

    def test_plain_from(self) -> None:
        self.assertEqual(self.refs("FROM alpine:3.22\n"), ["alpine:3.22"])

    def test_platform_flag_and_lowercase_keyword(self) -> None:
        self.assertEqual(
            self.refs("from --platform=$BUILDPLATFORM golang:1.23 AS build\n"),
            ["golang:1.23"],
        )

    def test_global_arg_default(self) -> None:
        # The test-docker-monorepo chain-child idiom.
        text = "ARG BASE_IMAGE=base-alpine:verify\n# hadolint ignore=DL3006\nFROM ${BASE_IMAGE}\n"
        self.assertEqual(self.refs(text), ["base-alpine:verify"])

    def test_build_arg_overrides_default(self) -> None:
        text = "ARG BASE_IMAGE=base-alpine:verify\nFROM $BASE_IMAGE\n"
        self.assertEqual(
            self.refs(text, BASE_IMAGE="onap/base-alpine:1.2.3"),
            ["onap/base-alpine:1.2.3"],
        )

    def test_build_arg_supplies_undeclared_default(self) -> None:
        self.assertEqual(self.refs("ARG BASE\nFROM ${BASE}\n", BASE="b:1"), ["b:1"])

    def test_multiple_and_quoted_arg_declarations(self) -> None:
        text = 'ARG REG="nexus3.onap.org:10001" NAME=onap/base\nFROM ${REG}/${NAME}:1\n'
        self.assertEqual(self.refs(text), ["nexus3.onap.org:10001/onap/base:1"])

    def test_arg_referencing_earlier_arg(self) -> None:
        text = "ARG TAG=1.0\nARG BASE=base:${TAG}\nFROM ${BASE}\n"
        self.assertEqual(self.refs(text), ["base:1.0"])

    def test_default_operators(self) -> None:
        self.assertEqual(expand("${X:-fallback}", {}), "fallback")
        self.assertEqual(expand("${X:-fallback}", {"X": ""}), "fallback")
        self.assertEqual(expand("${X-fallback}", {"X": ""}), "")
        self.assertEqual(expand("${X:+set}", {"X": "1"}), "set")
        self.assertEqual(expand("${X:+set}", {}), "")

    def test_pattern_operators(self) -> None:
        v = {"P": "local-base", "F": "a.b.c", "R": "x-y-z"}
        cases = {
            "${P#local-}": "base",
            "${P#*-}": "base",
            "${F#*.}": "b.c",
            "${F##*.}": "c",
            "${F%.*}": "a.b",
            "${F%%.*}": "a",
            "${F%nomatch}": "a.b.c",
            "${R/-/_}": "x_y-z",
            "${R//-/_}": "x_y_z",
            "${R/-*/}": "x",
            "img-${P#local-}:1": "img-base:1",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expand(text, v), expected)

    def test_patterns_follow_buildkit_not_fnmatch(self) -> None:
        # Only * and ? are wildcards: brackets are literal, and escapes
        # are limited to \*, \? and \\.
        cases = {
            ("${B#[a]}", "abc"): "abc",
            ("${B#[a]}", "[a]bc"): "bc",
            ("${B#?}", "abc"): "bc",
            ("${B#\\*}", "*x"): "x",
            ("${B#\\*}", "ax"): "ax",
            ("${B%\\*}", "x*"): "x",
            ("${B%?c}", "abc"): "a",
            ("${B##a*\\\\}", "ab\\c"): "c",
            ("${B/[x]/_}", "[x]y"): "_y",
            ("${B//a.b/-}", "a.bacb"): "-acb",
        }
        for (text, value), expected in cases.items():
            with self.subTest(text=text, value=value):
                self.assertEqual(expand(text, {"B": value}), expected)
        self.assertIsNone(expand("${B#\\a}", {"B": "abc"}), "invalid escape")

    def test_nested_and_lazy_words(self) -> None:
        self.assertEqual(expand("${A:-${B}}", {"B": "b"}), "b")
        self.assertEqual(expand("${A:-${B:-deep}}", {}), "deep")
        # An unused word needs no value, as in BuildKit.
        self.assertEqual(expand("${A:-${UNSET}}", {"A": "a"}), "a")
        self.assertEqual(expand("${A:-${UNSET}}", {}), "")

    def test_parameter_names_as_buildkit_reads_them(self) -> None:
        # docker build: ARG 1=... then FROM $1, and ${12}, both build.
        v = {"1": "base", "12": "twelve", "ÄRG": "umlaut", "a_1": "x"}
        cases = {
            "$1": "base",
            "${12}": "twelve",
            "$12x": "twelvex",
            "${ÄRG}": "umlaut",
            "$a_1-y": "x-y",
            "a$$b": "ab",
            "a$@b": "ab",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expand(text, v), expected)
        for text in ("${:}", "${}}", "${{X}"):
            with self.subTest(text=text):
                self.assertIsNone(expand(text, v))
        self.assertEqual(self.refs("ARG 1=base\nFROM $1:1\n"), ["base:1"])

    def test_escape_token_governs_expansion(self) -> None:
        # Under '# escape=`' the backtick escapes and '\' is literal.
        self.assertEqual(expand("a`-b", {}, "`"), "a-b")
        self.assertEqual(expand("a\\b", {}, "`"), "a\\b")
        self.assertEqual(expand('"a`"b"', {}, "`"), 'a"b')
        self.assertEqual(expand("ab\\", {}), "ab", "a trailing escape is dropped")
        text = "# escape=`\nARG BASE=local`-base\nFROM ${BASE}\n"
        self.assertEqual(self.refs(text), ["local-base"])

    def test_double_quotes_escape_only_quote_dollar_and_escape(self) -> None:
        # BuildKit's processDoubleQuote: backticks are not special.
        self.assertEqual(expand('"a\\`b"', {}), "a\\`b")
        self.assertEqual(expand('"a\\$b\\\\c"', {}), "a$b\\c")
        self.assertEqual(expand('"a\\xb"', {}), "a\\xb")

    def test_quoted_arg_default(self) -> None:
        self.assertEqual(self.refs('ARG BASE="base:1"\nFROM $BASE\n'), ["base:1"])
        self.assertEqual(self.refs("ARG BASE='base:1'\nFROM $BASE\n"), ["base:1"])

    def test_unset_expands_to_empty_as_in_buildkit(self) -> None:
        # docker build: ARG SUFFIX then FROM busybox${SUFFIX}:1.37 pulls
        # busybox:1.37.
        self.assertEqual(expand("base${SUFFIX}", {}), "base")
        self.assertEqual(expand("${X#p}", {}), "")
        self.assertEqual(self.refs("ARG SUFFIX\nFROM base${SUFFIX}:1\n"), ["base:1"])

    def test_quotes_as_buildkit_removes_them(self) -> None:
        # docker build: FROM ${BASE:-"busybox:1.37"} pulls busybox:1.37;
        # labels give ${UNSET:-"lit"} = lit, and quotes vanish mid-word.
        cases = {
            '${BASE:-"base"}': "base",
            'x${UNSET:-"y"}z': "xyz",
            "'${Q}'": "${Q}",
            '"${Q}-x"': "v-x",
            '"a \\"b\\""': 'a "b"',
            "${Q:+'s q'}": "s q",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expand(text, {"Q": "v"}), expected)
        self.assertIsNone(expand('"open', {}))
        self.assertEqual(self.refs('ARG BASE\nFROM ${BASE:-"base"}:1\n'), ["base:1"])

    def test_nested_expansion_does_not_end_the_outer_word(self) -> None:
        # docker build resolved ${S/${P/a/a}/b} with S=zzz to zzz.
        v = {"P": "abc", "S": "zzz", "B": "xabcx"}
        self.assertEqual(expand("${S/${P/a/a}/b}", v), "zzz")
        self.assertEqual(expand("${B/${P/a/a}/-}", v), "x-x")
        self.assertEqual(expand("${B/'}'/-}", {"B": "a}b"}), "a-b")

    def test_replace_all_follows_go_semantics(self) -> None:
        # Values docker build computes for B=abc.
        cases = {
            "${B//*/x}": "x",
            "${B//b*/x}": "ax",
            "${B//?/x}": "xxx",
            "${B/*/x}": "x",
            "${B//z*/x}": "abc",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expand(text, {"B": "abc"}), expected)

    def test_replace_all_expands_go_templates(self) -> None:
        # Labels docker build computes for B=a: '//' treats the
        # replacement as a Go template, while '/' splices it literally.
        cases = {
            ("//", "$1"): "",
            ("//", "[$0]"): "[a]",
            ("//", "$$"): "$",
            ("//", "x$"): "x$",
            ("//", "${0}y"): "ay",
            ("//", "$0x"): "",
            ("//", "$00"): "",
            ("/", "$1"): "$1",
            ("/", "[$0]"): "[$0]",
        }
        for (op, replacement), expected in cases.items():
            with self.subTest(op=op, replacement=replacement):
                text = f"${{B{op}a/${{R}}}}"
                self.assertEqual(expand(text, {"B": "a", "R": replacement}), expected)
        # The reviewer's case: the template empties it, leaving the base.
        self.assertEqual(expand("base${B//a/${R}}", {"B": "a", "R": "$1"}), "base")

    def test_unevaluable_forms_are_unresolved(self) -> None:
        for text in ("${X?}", "${X:#p}", "${X:0:2}", "${}", "${X", "${X/p}"):
            with self.subTest(text=text):
                self.assertIsNone(expand(text, {}))
        self.assertIsNone(expand("${X:0:2}", {"X": "abc"}))
        self.assertEqual(expand("${X?}", {"X": "v"}), "v")
        # $5 is a positional parameter, empty as in BuildKit; a '$' no
        # name follows stays literal.
        self.assertEqual(expand("cost $5", {}), "cost ")
        self.assertEqual(expand("cost $ 5 $", {}), "cost $ 5 $")

    def test_pattern_expansion_orders_by_sibling(self) -> None:
        text = "ARG BASE=local-base\nFROM ${BASE#local-}:verify\n"
        self.assertEqual(self.refs(text), ["base:verify"])

    def test_continuation_open_at_end_of_file(self) -> None:
        # docker build still builds a final FROM whose line ends with a
        # continuation marker, newline or not; dropping it would make
        # the previous stage the final one.
        for text in (
            "FROM a\nFROM b \\",
            "FROM a\nFROM b \\\n",
            "FROM a\nFROM \\\n  b\\",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.refs(text), ["b"])

    def test_only_lf_ends_a_physical_line(self) -> None:
        # BuildKit splits on LF and trims CR; VT, FF, NEL and U+2028 are
        # ordinary characters, so the FROM after them is argument text.
        for separator in ("\v", "\f", "\x85", "\u2028", "\r"):
            with self.subTest(separator=repr(separator)):
                text = f"FROM a\nRUN echo x{separator}FROM hidden:1\n"
                self.assertEqual(self.refs(text), ["a"])
        self.assertEqual(self.refs("FROM a\r\nFROM b\r\n"), ["b"])

    def test_run_mount_sources(self) -> None:
        # docker build: a bind mount's from= names a stage or an image;
        # a quoted CSV field works, and so do several mounts per RUN.
        stage = "FROM base:1 AS build\nFROM runtime:1\n"
        for run in (
            "RUN --mount=type=bind,from=build,target=/m true",
            'RUN --mount=type=bind,"from=build",target=/n true',
            "RUN --network=none --mount=type=cache,target=/c "
            "--mount=from=build,target=/n,type=bind true",
        ):
            with self.subTest(run=run):
                self.assertEqual(self.refs(stage + run + "\n"), ["base:1", "runtime:1"])
        image = "FROM a\nRUN --mount=type=bind,from=sibling:1,target=/m x\n"
        self.assertEqual(self.refs(image), ["a", "sibling:1"])

    def test_numeric_mount_source_is_an_image(self) -> None:
        # Unlike COPY --from=0, docker build pulls from=0 as image '0'.
        text = "FROM early:1\nFROM late:1\nRUN --mount=type=bind,from=0,target=/o x\n"
        self.assertEqual(self.refs(text), ["late:1", "0"])

    def test_flags_only_lead_an_instruction(self) -> None:
        # docker build pulled neither image: past the first word these
        # are ordinary arguments.
        text = (
            "FROM a\n"
            "RUN echo --mount=type=bind,from=x:1,target=/m\n"
            "COPY f --from=y:2 /\n"
        )
        self.assertEqual(self.refs(text), ["a"])

    def test_from_flags_do_not_expand(self) -> None:
        # docker build: "variable expansion is not supported for --from",
        # and likewise for a mount's from=.
        for line in (
            "COPY --from=${SRC} /x /x",
            "RUN --mount=type=bind,from=${SRC},target=/m true",
        ):
            with self.subTest(line=line):
                found = references(
                    f"ARG SRC=build\nFROM a AS build\nFROM b\n{line}\n", {}
                )
                self.assertEqual(found.images, ["b"])
                self.assertEqual(found.unresolved, ["${SRC}"])

    def test_byte_order_mark_is_ignored(self) -> None:
        self.assertEqual(self.refs("\ufeffFROM alpine:3\n"), ["alpine:3"])
        text = "\ufeff# escape=`\nFROM a `\n  AS b\nFROM b\n"
        self.assertEqual(self.refs(text), ["a"])

    def test_unset_variable_is_unresolved_not_guessed(self) -> None:
        found = references("FROM ${UNSET}\n", {})
        self.assertEqual(found.images, [])
        self.assertEqual(found.unresolved, ["${UNSET}"])

    def test_stage_arg_does_not_reach_from(self) -> None:
        # An ARG after the first FROM is stage-scoped.
        found = references("FROM a\nARG LATER=b\nFROM ${LATER}\n", {})
        self.assertEqual(found.images, [])
        self.assertEqual(found.unresolved, ["${LATER}"])

    def test_stage_names_and_scratch_skipped(self) -> None:
        text = (
            "FROM golang:1.23 AS Build\n"
            "FROM build AS test\n"
            "FROM scratch\n"
            "COPY --from=build /out /out\n"
            "COPY --from=0 /x /x\n"
            "COPY --from=tools:1 /bin/t /t\n"
        )
        self.assertEqual(self.refs(text), ["golang:1.23", "tools:1"])

    def test_from_alias_matching_its_image_pulls_the_image(self) -> None:
        self.assertEqual(self.refs("FROM base AS base\nRUN true\n"), ["base"])

    def test_continuations_and_comments(self) -> None:
        text = "FROM \\\n  # a comment inside a continuation\n  alpine:3 \\\n  AS base\nFROM base\n"
        self.assertEqual(self.refs(text), ["alpine:3"])

    # Each heredoc case ends on the text it must hide: parsed as an
    # instruction, it would become the final stage and the result.

    def test_heredoc_body_is_not_parsed(self) -> None:
        text = "FROM alpine:3\nRUN <<EOF\nFROM evil:1\nEOF\nCOPY <<-'DATA' /f\nFROM also-not:1\nDATA\n"
        self.assertEqual(self.refs(text), ["alpine:3"])

    def test_heredoc_delimiters_beyond_identifiers(self) -> None:
        for opener, delimiter in (
            ("<<MY-DELIM", "MY-DELIM"),
            ('3<<"END OF DATA" cat', "END OF DATA"),
        ):
            with self.subTest(opener=opener):
                text = f"FROM a\nRUN {opener}\nFROM hidden:1\n{delimiter}\n"
                self.assertEqual(self.refs(text), ["a"])

    def test_heredoc_terminator_must_match_exactly(self) -> None:
        # Without '-', an indented or padded terminator is body text.
        text = "FROM a\nRUN <<EOF\n  EOF\nEOF \nFROM hidden:1\nEOF\n"
        self.assertEqual(self.refs(text), ["a"])

    def test_dash_heredoc_strips_leading_tabs_only(self) -> None:
        spaces = "FROM a\nRUN <<-EOF\n  EOF\nFROM hidden:1\n"
        self.assertEqual(self.refs(spaces), ["a"])
        tabs = "FROM a\nRUN <<-EOF\n\t\tEOF\nFROM b\n"
        self.assertEqual(self.refs(tabs), ["b"])

    def test_quoted_heredoc_marker_is_an_argument(self) -> None:
        text = 'FROM a\nRUN echo "a <<b"\nFROM c\n'
        self.assertEqual(self.refs(text), ["c"])

    def test_spaced_heredoc_operator(self) -> None:
        # docker build treats RUN << EOF as a heredoc (verified with a
        # marker file the body creates).
        for opener in ("<< EOF", "<<- EOF", "3<< EOF", '<< "EOF"'):
            with self.subTest(opener=opener):
                text = f"FROM a\nRUN cat {opener}\nFROM hidden:1\nEOF\n"
                self.assertEqual(self.refs(text), ["a"])

    def test_spaced_dash_belongs_to_the_delimiter(self) -> None:
        # In RUN << -EOF the delimiter is '-EOF', without tab stripping,
        # so a tab-indented EOF does not end it (verified with docker).
        text = "FROM a\nRUN cat << -EOF\n\tEOF\nFROM hidden:1\n-EOF\n"
        self.assertEqual(self.refs(text), ["a"])

    def test_here_string_is_not_a_heredoc(self) -> None:
        # <<<EOF is a shell here-string: the next line is an instruction.
        self.assertEqual(self.refs("FROM a\nRUN cat <<<EOF\nFROM b\n"), ["b"])

    def test_onbuild_heredoc_body_is_not_parsed(self) -> None:
        text = "FROM a\nONBUILD RUN <<EOF\nFROM hidden:1\nEOF\n"
        self.assertEqual(self.refs(text), ["a"])

    def test_json_form_opens_no_heredoc(self) -> None:
        text = 'FROM a\nRUN ["sh", "-c", "cat <<EOF"]\nFROM b\n'
        self.assertEqual(self.refs(text), ["b"])

    def test_backtick_escape_directive(self) -> None:
        text = "# escape=`\nFROM base:1 `\n  AS build\nFROM build\n"
        self.assertEqual(self.refs(text), ["base:1"])
        # A trailing backslash is then literal, not a continuation.
        self.assertEqual(self.refs("# escape=`\nFROM a\\\nFROM b\n"), ["b"])

    def test_directive_after_a_comment_is_a_comment(self) -> None:
        text = "# a comment\n# escape=`\nFROM a `\nFROM b\n"
        self.assertEqual(self.refs(text), ["b"])

    def test_blank_lines_inside_a_continuation(self) -> None:
        text = "FROM \\\n\n  alpine:3 AS base\nFROM base\n"
        self.assertEqual(self.refs(text), ["alpine:3"])

    def test_only_stages_the_build_reaches_count(self) -> None:
        text = (
            "FROM base:1 AS build\n"
            "FROM unused:1 AS lint\n"
            "FROM runtime:1\n"
            "COPY --from=build /app /app\n"
        )
        self.assertEqual(self.refs(text), ["base:1", "runtime:1"])

    def test_target_limits_references_to_its_stages(self) -> None:
        text = "FROM early:1 AS build\nFROM late:1 AS final\nCOPY --from=build / /\n"
        self.assertEqual(references(text, {}, "build").images, ["early:1"])
        self.assertEqual(references(text, {}, "FINAL").images, ["early:1", "late:1"])

    def test_numeric_from_follows_the_stage(self) -> None:
        text = "FROM early:1\nFROM unused:1\nFROM late:1\nCOPY --from=0 / /\n"
        self.assertEqual(self.refs(text), ["early:1", "late:1"])

    def test_unknown_target_keeps_every_stage(self) -> None:
        text = "FROM a:1 AS one\nFROM b:1 AS two\n"
        self.assertEqual(references(text, {}, "missing").images, ["a:1", "b:1"])

    def test_build_arg_values(self) -> None:
        self.assertEqual(
            build_arg_values(["A=1", "B=x=y", "BARE"]), {"A": "1", "B": "x=y"}
        )


class GraphTest(unittest.TestCase):
    """Matching, ordering, levels and closure."""

    def test_split_reference(self) -> None:
        cases = {
            "base": ("", "base"),
            "base:verify": ("", "base"),
            "onap/base:1.2": ("", "onap/base"),
            "nexus3.onap.org:10001/onap/base:1.2": (
                "nexus3.onap.org:10001",
                "onap/base",
            ),
            "localhost:5000/base": ("localhost:5000", "base"),
            "localhost/base": ("localhost", "base"),
            "base@sha256:" + "0" * 64: ("", "base"),
            "onap/base:1@sha256:" + "0" * 64: ("", "onap/base"),
            # Docker Hub's spellings normalise away.
            "docker.io/library/base:1": ("", "base"),
            "docker.io/base:1": ("", "base"),
            "index.docker.io/library/base": ("", "base"),
            "index.docker.io/onap/base": ("", "onap/base"),
            # Hub's endpoint, not an alias: docker build does not map it
            # to a local tag.
            "registry-1.docker.io/library/base": (
                "registry-1.docker.io",
                "library/base",
            ),
            "BASE:1": ("", "base"),
        }
        for reference, expected in cases.items():
            with self.subTest(reference=reference):
                self.assertEqual(split_reference(reference), expected)

    def test_only_references_this_build_produces_match(self) -> None:
        names = ["base", "app"]
        # Unqualified, or under the namespace on any registry.
        for ref, namespace, expected in (
            ("base:verify", "", [0]),
            ("docker.io/library/base", "", [0]),
            ("docker.io/base", "", [0]),
            ("index.docker.io/library/base", "", [0]),
            ("registry-1.docker.io/library/base", "", []),
            ("onap/base:1", "onap", [0]),
            ("nexus3.onap.org:10001/onap/base:1", "onap", [0]),
            ("base:verify", "onap", [0]),
            ("onap/base:1", "", []),
            ("other/base:1", "onap", []),
            ("ghcr.io/vendor/base:1", "", []),
            ("ghcr.io/base:1", "", []),
            ("onap/sub/base:1", "onap", []),
        ):
            with self.subTest(ref=ref, namespace=namespace):
                deps = local_dependencies(names, [[], [ref]], namespace)
                self.assertEqual(deps[1], expected)

    def test_registry_qualified_namespace(self) -> None:
        # image_namespace: ghcr.io/org tags ghcr.io/org/<name>; the
        # registry is part of the namespace, so it must match as well.
        names = ["base", "app"]
        for ref, expected in (
            ("ghcr.io/org/base:1", [0]),
            ("GHCR.IO/org/base", [0]),
            ("base:verify", [0]),
            ("quay.io/org/base:1", []),
            ("org/base:1", []),
            ("ghcr.io/other/base:1", []),
        ):
            with self.subTest(ref=ref):
                self.assertEqual(
                    local_dependencies(names, [[], [ref]], "ghcr.io/org")[1], expected
                )

    def test_foreign_name_cannot_fabricate_a_cycle(self) -> None:
        # app really builds from tool; tool pulls a third party's 'app'.
        deps = local_dependencies(
            ["app", "tool"], [["tool:verify"], ["ghcr.io/vendor/app:1"]]
        )
        self.assertEqual(topological_order(["app", "tool"], deps), [1, 0])

    def test_local_dependencies_ignore_external_and_self(self) -> None:
        deps = local_dependencies(
            ["base", "child", "self"],
            [["alpine:3"], ["onap/base:1"], ["self:old"]],
            "onap",
        )
        self.assertEqual(deps, [[], [0], []])

    def test_order_is_stable_when_already_correct(self) -> None:
        names = ["base", "child", "util"]
        self.assertEqual(topological_order(names, [[], [0], []]), [0, 1, 2])

    def test_order_moves_base_ahead_of_child(self) -> None:
        # Declared child-first: only the base moves; util keeps its place.
        names = ["util", "child", "base"]
        self.assertEqual(topological_order(names, [[], [2], []]), [0, 2, 1])

    def test_cycle_is_an_error_naming_members(self) -> None:
        with self.assertRaisesRegex(ActionError, "cycle between: a, b$"):
            topological_order(["a", "b", "c"], [[1], [0], []])

    def test_cycle_report_separates_blocked_images(self) -> None:
        # a <-> b, and c builds from a: c is blocked, not a member.
        with self.assertRaisesRegex(
            ActionError, r"cycle between: a, b \(also blocked: c\)$"
        ):
            topological_order(["a", "b", "c"], [[1], [0], [0]])

    def test_separate_cycles_are_reported_apart(self) -> None:
        with self.assertRaisesRegex(ActionError, "cycle between: a, b; c, d$"):
            topological_order(["a", "b", "c", "d"], [[1], [0], [3], [2]])

    def test_levels_group_independent_images(self) -> None:
        deps = [[], [0], [], [1, 2]]
        order = topological_order(["a", "b", "c", "d"], deps)
        self.assertEqual(build_levels(order, deps), [[0, 2], [1], [3]])

    def test_closure_pulls_in_transitive_bases(self) -> None:
        deps = [[], [0], [1], []]
        self.assertEqual(with_dependencies({2}, deps), {0, 1, 2})


if __name__ == "__main__":
    unittest.main()
