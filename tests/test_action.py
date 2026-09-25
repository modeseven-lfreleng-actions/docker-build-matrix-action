# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""End-to-end behaviour of the action entry point.

Everything here goes beyond the inline implementation the lanes
carry: tunables, the richer outputs and input validation. Defaults
are proven equivalent to that implementation in test_equivalence.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

from tests.support import DOCKERFILE, TreeTestCase, invoke, run_action, workspace

D = DOCKERFILE

# A replica of test-docker-monorepo@v0.1.0's Dockerfiles, reduced to
# the lines that decide ordering.
MONOREPO = {
    "base-alpine/Dockerfile": "FROM alpine:3.22\n",
    "chain-child/Dockerfile": "ARG BASE_IMAGE=base-alpine:verify\n# hadolint ignore=DL3006\nFROM ${BASE_IMAGE}\n",
    "util-echo/Dockerfile": "FROM busybox:1.37\n",
}
REVERSED = json.dumps(
    [
        {"name": "util-echo", "context": "util-echo"},
        {"name": "chain-child", "context": "chain-child"},
        {"name": "base-alpine", "context": "base-alpine"},
    ]
)


def names(run: Any) -> list[str]:
    return [image["name"] for image in json.loads(run.outputs["images_json"])]


def matrix(run: Any) -> list[dict[str, Any]]:
    return json.loads(run.outputs["matrix"])["include"]


class OutputContractTest(TreeTestCase):
    """Every output is present and consistent."""

    def test_default_outputs(self) -> None:
        self.tree(MONOREPO)
        run = run_action(self.workspace)
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(
            set(run.outputs),
            {
                "images_json",
                "image_count",
                "image_names",
                "matrix",
                "build_levels",
                "build_id",
                "source",
            },
        )
        self.assertEqual(run.outputs["image_count"], "3")
        self.assertEqual(
            run.outputs["image_names"], "base-alpine chain-child util-echo"
        )
        self.assertEqual(run.outputs["source"], "discovered")
        # Declared order promises nothing about independence: one image
        # per stage, in order.
        self.assertEqual(
            json.loads(run.outputs["build_levels"]),
            [["base-alpine"], ["chain-child"], ["util-echo"]],
        )
        self.assertEqual([entry["index"] for entry in matrix(run)], [0, 1, 2])

    def test_matrix_entry_fields(self) -> None:
        self.tree({"sub/proj/a/Dockerfile.alt": D})
        images = json.dumps(
            [
                {
                    "name": "a",
                    "context": "a",
                    "dockerfile": "a/Dockerfile.alt",
                    "platforms": "linux/arm64",
                    "smoke": "true",
                },
                {"name": "b", "context": "."},
            ]
        )
        run = run_action(
            self.workspace,
            path_prefix="sub/proj",
            images=images,
            image_namespace="onap",
            platforms="linux/amd64",
        )
        self.assertEqual(run.status, 0, run.stdout)
        first, second = matrix(run)
        self.assertEqual(first["image"], "onap/a")
        self.assertEqual(first["context_path"], "sub/proj/a")
        self.assertEqual(first["dockerfile_path"], "sub/proj/a/Dockerfile.alt")
        self.assertEqual(first["platforms"], "linux/arm64")
        self.assertEqual(first["smoke"], "true", "unknown keys pass through")
        self.assertEqual(second["dockerfile"], "./Dockerfile")
        self.assertEqual(second["dockerfile_path"], "sub/proj/Dockerfile")
        self.assertEqual(
            second["platforms"], "linux/amd64", "falls back to the action input"
        )
        self.assertEqual(
            (second["target"], second["build_args"], second["depends_on"]), ("", [], [])
        )
        self.assertEqual(run.outputs["source"], "explicit")
        # images_json stays the lanes' contract: the input, not the matrix.
        self.assertNotIn("image", json.loads(run.outputs["images_json"])[0])

    def test_summary_table(self) -> None:
        self.tree(MONOREPO)
        plain = run_action(self.workspace)
        self.assertIn("| Image | Dockerfile | Context |\n", plain.summary)
        ordered = run_action(self.workspace, order="dependencies")
        self.assertIn(
            "| chain-child | chain-child/Dockerfile | chain-child | base-alpine |",
            ordered.summary,
        )
        self.assertEqual(run_action(self.workspace, summary="false").summary, "")


class OrderingTest(TreeTestCase):
    """order: dependencies builds bases before the images using them."""

    def test_reversed_explicit_list_is_reordered(self) -> None:
        self.tree(MONOREPO)
        run = run_action(self.workspace, images=REVERSED, order="dependencies")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(names(run), ["util-echo", "base-alpine", "chain-child"])
        self.assertEqual(
            json.loads(run.outputs["build_levels"]),
            [["util-echo", "base-alpine"], ["chain-child"]],
        )
        child = matrix(run)[2]
        self.assertEqual((child["depends_on"], child["level"]), (["base-alpine"], 1))

    def test_declared_order_keeps_the_callers_order(self) -> None:
        self.tree(MONOREPO)
        self.assertEqual(
            names(run_action(self.workspace, images=REVERSED)),
            ["util-echo", "chain-child", "base-alpine"],
        )

    def test_discovery_order_corrected_when_sorting_is_wrong(self) -> None:
        # Sorted discovery puts app before zbase; its FROM needs zbase.
        self.tree({"app/Dockerfile": "FROM onap/zbase:1.0\n", "zbase/Dockerfile": D})
        self.assertEqual(names(run_action(self.workspace)), ["app", "zbase"])
        self.assertEqual(
            names(
                run_action(self.workspace, order="dependencies", image_namespace="onap")
            ),
            ["zbase", "app"],
        )

    def test_foreign_namespaces_are_not_siblings(self) -> None:
        # onap/zbase is only this build's zbase when the build tags
        # images under onap.
        self.tree({"app/Dockerfile": "FROM onap/zbase:1.0\n", "zbase/Dockerfile": D})
        self.assertEqual(
            names(run_action(self.workspace, order="dependencies")), ["app", "zbase"]
        )

    def test_foreign_image_sharing_a_name_is_no_cycle(self) -> None:
        # tool really builds from app; ghcr.io/vendor/tool is never the
        # local tool, so there is no edge back and no false cycle.
        self.tree(
            {
                "app/Dockerfile": "FROM ghcr.io/vendor/tool:1\n",
                "tool/Dockerfile": "FROM app\n",
            }
        )
        run = run_action(self.workspace, order="dependencies")
        self.assertEqual(run.status, 0, run.annotations)
        self.assertEqual(names(run), ["app", "tool"])

    def test_registry_qualified_same_repository_base(self) -> None:
        # The ONAP idiom: a sibling pulled back through the project's own
        # registry path, which the namespace identifies as this build's.
        self.tree(
            {
                "a/Dockerfile": "FROM nexus3.onap.org:10001/onap/b:${TAG:-1.0}\n",
                "b/Dockerfile": D,
            }
        )
        self.assertEqual(
            names(
                run_action(self.workspace, order="dependencies", image_namespace="onap")
            ),
            ["b", "a"],
        )

    def test_build_args_steer_resolution(self) -> None:
        self.tree(MONOREPO)
        images = json.dumps(
            [
                {
                    "name": "chain-child",
                    "context": "chain-child",
                    "build_args": ["BASE_IMAGE=util-echo:verify"],
                },
                {"name": "util-echo", "context": "util-echo"},
            ]
        )
        self.assertEqual(
            names(run_action(self.workspace, images=images, order="dependencies")),
            ["util-echo", "chain-child"],
        )

    def test_cycle_fails(self) -> None:
        self.tree({"a/Dockerfile": "FROM b\n", "b/Dockerfile": "FROM a\n"})
        run = run_action(self.workspace, order="dependencies")
        self.assertEqual(run.status, 1)
        self.assertEqual(
            run.annotations, ["::error::Image dependency cycle between: a, b"]
        )

    def test_target_skips_stages_the_build_never_reaches(self) -> None:
        # a builds only its early stage; the later FROM b is never built,
        # so b building from a is not a cycle.
        self.tree(
            {
                "a/Dockerfile": "FROM alpine:3 AS build\nFROM b AS extras\n",
                "b/Dockerfile": "FROM a\n",
            }
        )
        images = json.dumps(
            [
                {"name": "b", "context": "b"},
                {"name": "a", "context": "a", "target": "build"},
            ]
        )
        run = run_action(self.workspace, images=images, order="dependencies")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(names(run), ["a", "b"])

    def test_unresolvable_reference_warns(self) -> None:
        self.tree({"a/Dockerfile": "FROM ${BASE}\n"})
        run = run_action(self.workspace, order="dependencies")
        self.assertEqual(run.status, 0)
        self.assertIn("cannot resolve '${BASE}'", run.annotations[0])

    def test_missing_dockerfile_warns(self) -> None:
        self.tree({"a/x": "x"})
        run = run_action(
            self.workspace, images='[{"name":"a","context":"a"}]', order="dependencies"
        )
        self.assertEqual(run.status, 0)
        self.assertTrue(
            run.annotations[0].startswith("::warning::a: cannot read a/Dockerfile")
        )


class SelectionTest(TreeTestCase):
    """select narrows the set, pulling in bases by default."""

    def test_select_pulls_in_same_repository_base(self) -> None:
        self.tree(MONOREPO)
        run = run_action(self.workspace, select="chain-child")
        self.assertEqual(names(run), ["base-alpine", "chain-child"])

    def test_select_without_dependencies(self) -> None:
        self.tree(MONOREPO)
        run = run_action(
            self.workspace, select="chain-child", select_dependencies="false"
        )
        self.assertEqual(names(run), ["chain-child"])

    def test_select_without_dependencies_survives_ordering(self) -> None:
        # order: dependencies reads the graph too; it must not re-add bases.
        self.tree(MONOREPO)
        run = run_action(
            self.workspace,
            select="chain-child",
            select_dependencies="false",
            order="dependencies",
        )
        self.assertEqual(names(run), ["chain-child"])
        self.assertEqual(matrix(run)[0]["depends_on"], [])

    def test_globs_commas_and_case(self) -> None:
        self.tree(MONOREPO)
        run = run_action(
            self.workspace, select="UTIL-*, base-alpine", select_dependencies="false"
        )
        self.assertEqual(names(run), ["base-alpine", "util-echo"])

    def test_unmatched_pattern_warns(self) -> None:
        self.tree(MONOREPO)
        run = run_action(self.workspace, select="util-echo nope")
        self.assertEqual(run.status, 0)
        self.assertIn(
            "::warning::select pattern 'nope' matched no image", run.annotations
        )

    def test_matching_nothing_fails_unless_allowed(self) -> None:
        self.tree(MONOREPO)
        failed = run_action(self.workspace, select="nope")
        self.assertEqual(failed.status, 1)
        self.assertIn(
            "::error::select (nope) matched none of the 3 image(s)", failed.annotations
        )
        allowed = run_action(self.workspace, select="nope", allow_empty="true")
        self.assertEqual(allowed.status, 0)
        self.assertEqual(
            (allowed.outputs["image_count"], allowed.outputs["matrix"]),
            ("0", '{"include":[]}'),
        )


class DiscoveryTunablesTest(TreeTestCase):
    """search_depth, name_from, exclude_paths and allow_empty."""

    NESTED = {
        "services/api/Dockerfile": D,
        "services/web/Dockerfile": D,
        "tools/api/Dockerfile": D,
        "tests/fixture/Dockerfile": D,
        "docker/base/Dockerfile": D,
    }

    def test_depth_one_ignores_nested(self) -> None:
        self.tree(self.NESTED)
        self.assertEqual(run_action(self.workspace).status, 1)

    def test_deeper_walk_with_directory_names(self) -> None:
        self.tree(self.NESTED)
        run = run_action(self.workspace, search_depth="2")
        # tools/api repeats "api": the first occurrence wins, as ever.
        self.assertEqual(names(run), ["base", "api", "web", "fixture"])
        self.assertIn(
            "Skipping tools/api/Dockerfile: image name 'api' is already taken",
            run.stdout,
        )

    def test_path_names_are_unique(self) -> None:
        self.tree(self.NESTED)
        run = run_action(self.workspace, search_depth="2", name_from="path")
        self.assertEqual(
            names(run),
            [
                "docker-base",
                "services-api",
                "services-web",
                "tests-fixture",
                "tools-api",
            ],
        )

    def test_path_name_collisions_fail(self) -> None:
        for files, depth, clash in (
            ({"a/b/Dockerfile": D, "a-b/Dockerfile": D}, "2", "'a-b' from a/b and a-b"),
            # Sanitising merges these (not API/api: some filesystems
            # fold case, making those one directory).
            ({"a b/Dockerfile": D, "a+b/Dockerfile": D}, "1", "'a-b' from a b and a+b"),
            (
                {"Dockerfile": D, "example-repo/Dockerfile": D},
                "1",
                "'example-repo' from . and example-repo",
            ),
        ):
            with self.subTest(files=sorted(files)), workspace(files) as root:
                run = run_action(root, search_depth=depth, name_from="path")
                self.assertEqual(run.status, 1, run.stdout)
                self.assertIn(clash, run.annotations[0])

    def test_project_locations_keep_first_wins_in_path_mode(self) -> None:
        self.tree({"Dockerfile": D, "docker/Dockerfile": D, "app/Dockerfile": D})
        run = run_action(self.workspace, name_from="path")
        self.assertEqual(names(run), ["example-repo", "app"])

    def test_exclude_paths_prune(self) -> None:
        self.tree(self.NESTED)
        run = run_action(
            self.workspace,
            search_depth="2",
            name_from="path",
            exclude_paths="./tests/\ntools/*, docker",
        )
        self.assertEqual(names(run), ["services-api", "services-web"])

    def test_exclude_project_level_location(self) -> None:
        self.tree({"Dockerfile": D, "docker/Dockerfile": D, "app/Dockerfile": D})
        run = run_action(self.workspace, exclude_paths=".")
        self.assertEqual(
            json.loads(run.outputs["images_json"])[0]["dockerfile"], "docker/Dockerfile"
        )

    def test_excluding_a_parent_covers_fixed_locations(self) -> None:
        self.tree({"src/main/docker/Dockerfile": D, "app/Dockerfile": D})
        for pattern in ("src", "src/main", "s*"):
            with self.subTest(pattern=pattern):
                run = run_action(self.workspace, exclude_paths=pattern)
                self.assertEqual(names(run), ["app"])

    def test_symlink_loop_is_not_followed(self) -> None:
        # a/loop -> a: the link is a candidate once, never descended.
        self.tree({"a/Dockerfile": D})
        os.symlink(".", self.workspace / "a" / "loop")
        run = run_action(self.workspace, search_depth="6", name_from="path")
        self.assertEqual(names(run), ["a", "a-loop"])

    def test_allow_empty(self) -> None:
        self.tree({"README.md": "x"})
        run = run_action(self.workspace, allow_empty="true")
        self.assertEqual(run.status, 0)
        self.assertEqual(run.outputs["image_count"], "0")
        self.assertEqual(json.loads(run.outputs["build_levels"]), [])

    def test_discovery_tunables_ignored_with_explicit_images(self) -> None:
        self.tree({"a/Dockerfile": D})
        run = run_action(
            self.workspace, images='[{"name":"a","context":"a"}]', search_depth="3"
        )
        self.assertEqual(run.status, 0)
        self.assertEqual(
            run.annotations,
            [
                "::notice::Ignoring discovery-only input(s) with an explicit images input: search_depth"
            ],
        )


class BuildIdTest(TreeTestCase):
    """The configuration stem moves only when a tunable does."""

    def stem(self, **inputs: str) -> str:
        return run_action(self.workspace, **inputs).outputs["build_id"][:12]

    def test_default_stem_matches_the_lanes_hash(self) -> None:
        self.tree(MONOREPO)
        fields = [
            "example-org/example-repo",
            "refs/heads/main",
            "",
            ".",
            "",
            "",
            "",
            "",
            "",
        ]
        expected = hashlib.sha256("|".join(fields).encode()).hexdigest()[:12]
        self.assertEqual(self.stem(), expected)

    def test_tunables_change_the_stem(self) -> None:
        self.tree(MONOREPO)
        base = self.stem()
        self.assertEqual(
            self.stem(allow_empty="true", summary="false"),
            base,
            "no effect on what is built",
        )
        for inputs in (
            {"order": "dependencies"},
            {"select": "util-echo"},
            {"search_depth": "2"},
            {"ref": "v1"},
        ):
            with self.subTest(inputs=inputs):
                self.assertNotEqual(self.stem(**inputs), base)


class ValidationTest(TreeTestCase):
    """Misconfiguration fails fast with a message naming the input."""

    def test_rejected_inputs(self) -> None:
        self.tree({"a/Dockerfile": D})
        cases = {
            "search_depth must be an integer from 1 to 32; got '0'": {
                "search_depth": "0"
            },
            "search_depth must be an integer from 1 to 32; got 'two'": {
                "search_depth": "two"
            },
            "order must be one of declared, dependencies; got 'topo'": {
                "order": "topo"
            },
            "name_from must be one of directory, path; got 'dir'": {"name_from": "dir"},
            "allow_empty must be 'true' or 'false', got 'maybe'": {
                "allow_empty": "maybe"
            },
            "path_prefix 'missing' is not a directory": {"path_prefix": "missing"},
        }
        for message, inputs in cases.items():
            with self.subTest(inputs=inputs):
                run = run_action(self.workspace, **inputs)
                self.assertEqual(
                    (run.status, run.annotations), (1, [f"::error::{message}"])
                )

    def test_invalid_namespace(self) -> None:
        self.tree({"a/Dockerfile": D})
        for namespace in (
            "ONAP",
            "onap/",
            "/onap",
            "on ap",
            ".",
            "-team",
            "team_",
            "team.",
            "a//b",
            "a___b",
        ):
            with self.subTest(namespace=namespace):
                run = run_action(self.workspace, image_namespace=namespace)
                self.assertEqual(run.status, 1)
                self.assertTrue(
                    run.annotations[0].startswith(
                        f"::error::Invalid image_namespace '{namespace}'"
                    )
                )

    def test_valid_namespace(self) -> None:
        self.tree({"a/Dockerfile": D})
        for namespace in ("onap", "my-org/sub_team", "a__b", "a--b.c", "0"):
            with self.subTest(namespace=namespace):
                run = run_action(self.workspace, image_namespace=namespace)
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(matrix(run)[0]["image"], f"{namespace}/a")


class ContainmentTest(TreeTestCase):
    """Paths stay inside the workspace; the Dockerfile reader too."""

    def test_path_prefix_must_stay_inside(self) -> None:
        self.tree({"sub/Dockerfile": D})
        with tempfile.TemporaryDirectory() as outside:
            os.symlink(outside, self.workspace / "link")
            for prefix in (outside, "..", "sub/../..", "link"):
                with self.subTest(prefix=prefix):
                    run = run_action(self.workspace, path_prefix=prefix)
                    self.assertEqual(run.status, 1)
                    self.assertIn(
                        "must be a path inside the workspace", run.annotations[0]
                    )

    def test_image_paths_must_stay_inside(self) -> None:
        self.tree({"services/api/Dockerfile": D, "shared/x": "x"})
        for entry in (
            {"name": "a", "context": ".."},
            {"name": "a", "context": "/etc"},
            {"name": "a", "context": ".", "dockerfile": "/etc/passwd"},
            {"name": "a", "context": ".", "dockerfile": "../../Dockerfile"},
        ):
            with self.subTest(entry=entry):
                run = run_action(self.workspace, images=json.dumps([entry]))
                self.assertEqual(run.status, 1)
                self.assertIn("must stay inside the workspace", run.annotations[0])

    def test_context_above_path_prefix_is_allowed(self) -> None:
        # A context wider than the Dockerfile's directory is normal usage.
        self.tree({"services/api/Dockerfile": D, "shared/x": "x"})
        entry = {"name": "api", "context": "../..", "dockerfile": "Dockerfile"}
        run = run_action(
            self.workspace, path_prefix="services/api", images=json.dumps([entry])
        )
        self.assertEqual(run.status, 0, run.annotations)
        self.assertEqual(matrix(run)[0]["context_path"], ".")

    def test_discovered_link_out_of_the_workspace_fails(self) -> None:
        self.tree({"app/Dockerfile": D})
        with tempfile.TemporaryDirectory() as outside:
            pathlib.Path(outside, "Dockerfile").write_text(D, encoding="utf-8")
            os.symlink(outside, self.workspace / "ext")
            run = run_action(self.workspace)
            self.assertEqual(run.status, 1)
            self.assertIn("ext (context 'ext')", run.annotations[0])


class PlatformsTest(TreeTestCase):
    """A per-image platforms key is honoured or refused, never dropped."""

    def test_string_list_and_absent(self) -> None:
        self.tree({"a/Dockerfile": D})
        for value, expected in (
            ("linux/arm64", "linux/arm64"),
            (["linux/amd64", "linux/arm64"], "linux/amd64,linux/arm64"),
            (None, "linux/amd64"),
            ("", "linux/amd64"),
            ([], "linux/amd64"),
            # Nothing but separators and spaces falls back too.
            (" ,", "linux/amd64"),
            (["", " "], "linux/amd64"),
            (" linux/arm64 , ,linux/arm/v7 ", "linux/arm64,linux/arm/v7"),
        ):
            with self.subTest(value=value):
                entry = {"name": "a", "context": "a", "platforms": value}
                run = run_action(
                    self.workspace, images=json.dumps([entry]), platforms="linux/amd64"
                )
                self.assertEqual(run.status, 0, run.annotations)
                self.assertEqual(matrix(run)[0]["platforms"], expected)

    def test_other_types_are_refused(self) -> None:
        self.tree({"a/Dockerfile": D})
        for value in (3, {"os": "linux"}, ["linux/amd64", 1], True, False):
            with self.subTest(value=value):
                entry = {"name": "a", "context": "a", "platforms": value}
                run = run_action(self.workspace, images=json.dumps([entry]))
                self.assertEqual(run.status, 1)
                self.assertIn("'platforms' is neither a string", run.annotations[0])

    def test_unparsable_platforms_are_refused(self) -> None:
        self.tree({"a/Dockerfile": D})
        entry = {"name": "a", "context": "a", "platforms": ["linux/amd64", "linux/*"]}
        for inputs in (
            {"platforms": "linux/amd64, notanarch"},
            {"images": json.dumps([entry])},
        ):
            with self.subTest(inputs=inputs):
                run = run_action(self.workspace, **inputs)
                self.assertEqual(run.status, 1)
                self.assertTrue(
                    run.annotations[0].startswith("::error::Invalid platform(s): "),
                    run.annotations,
                )

    def test_build_platform_is_one_specifier(self) -> None:
        self.tree({"a/Dockerfile": D})
        for builder in ("linux/*", "linux/arm64,linux/amd64", "linux/arm64,"):
            with self.subTest(builder=builder):
                run = run_action(self.workspace, build_platform=builder)
                self.assertEqual(run.status, 1)
                self.assertEqual(
                    run.annotations,
                    [
                        "::error::build_platform must be a single platform, "
                        f"such as linux/amd64; got '{builder}'"
                    ],
                )
        self.assertEqual(
            run_action(self.workspace, build_platform="linux/arm64").status, 0
        )


class RobustnessTest(TreeTestCase):
    """Hostile or odd input fails with an annotation, never a traceback."""

    def test_paths_cannot_inject_commands_or_markdown(self) -> None:
        # A line break in a path must not start a new log line, where
        # '::' would be a workflow command, nor break the summary table.
        self.tree({"a/Dockerfile": D})
        hostile = "a\n::error::injected\r\n| x | <b>y</b> |"
        entry = {"name": "a", "context": ".", "dockerfile": hostile}
        run = run_action(self.workspace, images=json.dumps([entry]))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.annotations, [])
        self.assertIn("(a\\n::error::injected\\r\\n| x | <b>y</b> |)", run.stdout)
        rows = [line for line in run.summary.splitlines() if line.startswith("| a ")]
        self.assertEqual(len(rows), 1, run.summary)
        self.assertIn(
            "a\\n::error::injected\\r\\n\\| x \\| &lt;b&gt;y&lt;/b&gt; \\|", rows[0]
        )
        self.assertNotIn("<b>", run.summary)

    def test_nul_in_a_path_is_outside_the_workspace(self) -> None:
        self.tree({"a/Dockerfile": D})
        for entry in (
            {"name": "a", "context": "a\u0000"},
            {"name": "a", "context": "a", "dockerfile": "a/\u0000"},
        ):
            with self.subTest(entry=entry):
                run = run_action(self.workspace, images=json.dumps([entry]))
                self.assertEqual(run.status, 1)
                self.assertIn("must stay inside the workspace", run.annotations[0])
                self.assertNotIn("Traceback", run.stdout)

    def test_non_ascii_digits_in_search_depth(self) -> None:
        self.tree({"a/Dockerfile": D})
        for depth in ("\u00b2", "\u0663", "1\u00b9", "9" * 5000, "0" * 4 + "1" * 4):
            with self.subTest(depth=depth):
                run = run_action(self.workspace, search_depth=depth)
                self.assertEqual(run.status, 1)
                self.assertTrue(run.annotations[0].startswith("::error::search_depth"))
                self.assertNotIn("Traceback", run.stdout)

    def test_unexpected_failure_still_annotates(self) -> None:
        from scripts import discover, gha

        # A bug while resolving, and one while publishing outputs.
        for target, attribute in ((discover, "resolve"), (gha, "set_outputs")):
            with self.subTest(fails=attribute):
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(
                        target, attribute, side_effect=RuntimeError("boom")
                    ),
                    mock.patch.dict(
                        os.environ, {"INPUT_IMAGES": '[{"name":"a","context":"."}]'}
                    ),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    self.assertEqual(discover.main(), 1)
                self.assertIn(
                    "::error::Internal error in docker-build-matrix-action: RuntimeError('boom')",
                    stdout.getvalue(),
                )
                self.assertIn("Traceback", stderr.getvalue())


class IsolationTest(TreeTestCase):
    """The repository under test cannot substitute its own code."""

    HOSTILE = (
        "import pathlib\n"
        "pathlib.Path('pwned').write_text('x')\n"
        "def main():\n    return 0\n"
    )

    def test_workspace_cannot_shadow_the_package(self) -> None:
        self.tree(
            {
                "a/Dockerfile": D,
                "scripts/__init__.py": "",
                "scripts/discover.py": self.HOSTILE,
                "scripts/gha.py": self.HOSTILE,
            }
        )
        # Prove the tree is hostile: the former invocation runs it.
        invoke([sys.executable, "-m", "scripts.discover"], self.workspace, {})
        self.assertTrue((self.workspace / "pwned").exists())
        (self.workspace / "pwned").unlink()

        run = run_action(self.workspace)
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["image_names"], "a")
        self.assertFalse((self.workspace / "pwned").exists())


class ImprovementsTest(TreeTestCase):
    """Deliberate departures from the inline body, where it misbehaved."""

    def test_several_invalid_names_share_one_annotation(self) -> None:
        # Inline, the second name spilled onto a plain log line.
        self.tree({"a/Dockerfile": D})
        run = run_action(
            self.workspace,
            images='[{"name":"-x","context":"a"},{"name":"y-","context":"a"}]',
        )
        self.assertEqual(
            run.annotations,
            [
                "::error::Invalid image name(s) after sanitisation: -x, y- (names must start/end with a-z or 0-9)"
            ],
        )

    def test_dot_slash_prefix_names_after_the_repository(self) -> None:
        # Inline, basename("./") produced the invalid name ".".
        self.tree({"Dockerfile": D})
        self.assertEqual(
            names(run_action(self.workspace, path_prefix="./")), ["example-repo"]
        )

    def test_equivalent_prefix_spellings_name_the_project(self) -> None:
        self.tree({"sub/Dockerfile": D, "sub/child/x": "x"})
        for prefix in ("sub", "sub/", "sub/.", "./sub", "sub/child/.."):
            with self.subTest(prefix=prefix):
                run = run_action(self.workspace, path_prefix=prefix)
                self.assertEqual(names(run), ["sub"], run.annotations)

    def test_empty_dockerfile_defaults_in_the_matrix(self) -> None:
        self.tree({"a/Dockerfile": D})
        run = run_action(
            self.workspace, images='[{"name":"a","context":"a","dockerfile":""}]'
        )
        self.assertEqual(matrix(run)[0]["dockerfile"], "a/Dockerfile")

    def test_names_docker_cannot_tag_are_rejected(self) -> None:
        # Inline, these passed and failed later, at docker tag.
        self.tree({"a/Dockerfile": D})
        for name in ("a..b", "a___b", "a-.b", "a._b"):
            with self.subTest(name=name):
                images = json.dumps([{"name": name, "context": "a"}])
                run = run_action(self.workspace, images=images)
                self.assertEqual(run.status, 1)
                self.assertTrue(
                    run.annotations[0].startswith(
                        f"::error::Invalid image name(s) after sanitisation: {name} "
                        "(separators must sit between alphanumerics"
                    ),
                    run.annotations,
                )
        for name in ("a.b", "a__b", "a---b", "web_ui"):
            with self.subTest(name=name):
                images = json.dumps([{"name": name, "context": "a"}])
                self.assertEqual(run_action(self.workspace, images=images).status, 0)

    def test_non_json_constants_are_rejected(self) -> None:
        # Inline, jq turned NaN into null; passed through here, it would
        # make images_json unparsable, so it is refused as invalid input.
        self.tree({"a/Dockerfile": D})
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                images = f'[{{"name":"a","context":"a","custom":{constant}}}]'
                run = run_action(self.workspace, images=images)
                self.assertEqual(run.status, 1)
                self.assertTrue(run.annotations[0].startswith("::error::images input"))

    def test_huge_numbers_pass_through_exactly(self) -> None:
        # A float cannot hold 1e400, but the literal is valid JSON, and
        # jq keeps it; so does the action, in both outputs.
        self.tree({"a/Dockerfile": D})
        images = '[{"name":"a","context":"a","n":1e400,"i":100000000000000000001}]'
        run = run_action(self.workspace, images=images)
        self.assertEqual(run.status, 0, run.annotations)
        self.assertIn(
            '"n":1E+400,"i":100000000000000000001', run.outputs["images_json"]
        )
        self.assertIn('"n":1E+400,"i":100000000000000000001', run.outputs["matrix"])

    def test_repository_length_limit(self) -> None:
        self.tree({"a/Dockerfile": D})
        for namespace, length, status in (
            ("", 255, 0),
            ("", 256, 1),
            ("onap", 250, 0),
            ("onap", 251, 1),
        ):
            with self.subTest(namespace=namespace, length=length):
                images = json.dumps([{"name": "a" * length, "context": "a"}])
                run = run_action(
                    self.workspace, images=images, image_namespace=namespace
                )
                self.assertEqual(run.status, status, run.annotations)
                if status:
                    self.assertIn("255-character limit", run.annotations[0])


if __name__ == "__main__":
    unittest.main()
