# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""BuildKit's automatic arguments and containerd's platform rules.

Expectations come from containerd's platforms package (Parse,
normalizeArch, FormatAll), which scripts/platforms.py ports, and from
docker build, which BuildKit's defaultArgs feeds. The host is mocked,
so results do not depend on where the tests run.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from scripts import platforms
from scripts.dockerfile import references
from tests.support import DOCKERFILE, TreeTestCase, run_action


def on_host(machine: str) -> Any:
    """Patch the host architecture platform.machine() reports."""
    return mock.patch.object(platforms._host, "machine", return_value=machine)


class ParseTest(unittest.TestCase):
    """containerd's Parse followed by Normalize, as BuildKit applies them."""

    def formats(self, spec: str) -> str | None:
        platform = platforms.resolve(spec)
        return platform.format_all() if platform else None

    def test_containerd_table(self) -> None:
        cases = {
            # Observed with docker build.
            "linux/arm/v7": "linux/arm/v7",
            "linux/arm": "linux/arm/v7",
            "linux/arm64/v8": "linux/arm64",
            "linux/amd64": "linux/amd64",
            "linux/x86_64": "linux/amd64",
            "linux/aarch64": "linux/arm64",
            # normalizeArch, case for case.
            "linux/arm64/v8.0": "linux/arm64",
            "linux/arm64/8": "linux/arm64",
            "linux/arm64/9.0": "linux/arm64/v9",
            "linux/arm64/v9.0": "linux/arm64/v9",
            "linux/amd64/3": "linux/amd64/3",
            "linux/amd64/v1": "linux/amd64",
            "linux/arm/7": "linux/arm/v7",
            "linux/arm/6": "linux/arm/v6",
            "linux/armhf": "linux/arm/v7",
            "linux/armel": "linux/arm/v6",
            "linux/i386/v2": "linux/386",
            "LINUX/AMD64": "linux/amd64",
            # One part: an architecture on Linux.
            "amd64": "linux/amd64",
            "x86_64": "linux/amd64",
            "arm": "linux/arm/v7",
            "riscv64": "linux/riscv64",
            # An OS version, which TARGETPLATFORM carries.
            "windows(10.0.17763)/amd64": "windows(10.0.17763)/amd64",
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                self.assertEqual(self.formats(spec), expected)

    def test_one_part_operating_systems_keep_the_host_arch(self) -> None:
        with on_host("aarch64"):
            for spec, expected in {
                "linux": "linux/arm64",
                "windows": "windows/arm64",
                "macos": "darwin/arm64",
                "zos": "zos/arm64",
                "hurd": "hurd/arm64",
                "nacl": "nacl/arm64",
            }.items():
                with self.subTest(spec=spec):
                    self.assertEqual(self.formats(spec), expected)

    def test_format_all_escapes_as_containerd(self) -> None:
        # osOptionReplacer escapes only % + ( ) /; features are sorted
        # and de-duplicated.
        for spec, expected in {
            "windows(10%3A0)/amd64": "windows(10:0)/amd64",
            "windows(10%2B1)/amd64": "windows(10%2B1)/amd64",
            "windows(a%2Fb%25c)/amd64": "windows(a%2Fb%25c)/amd64",
            "windows(10.0+b+a+a)/amd64": "windows(10.0+a+b)/amd64",
            "windows(+feat)/amd64": "windows(+feat)/amd64",
        }.items():
            with self.subTest(spec=spec):
                self.assertEqual(self.formats(spec), expected)

    def test_invalid_specifiers(self) -> None:
        for spec in (
            "linux/*",
            "notanarch",
            "linux/amd64/v1/extra/x",
            "lin ux/amd64",
            "",
            "windows(v%ZZ)/amd64",
            "windows(10%)/amd64",
            "windows(10+feat%2)/amd64",
        ):
            with self.subTest(spec=spec):
                self.assertIsNone(platforms.resolve(spec))
        # Well-formed escapes decode, as url.PathUnescape does.
        escaped = platforms.resolve("windows(10%2E0)/amd64")
        self.assertEqual(escaped.os_version if escaped else None, "10.0")

    def test_host_from_uname(self) -> None:
        for machine, expected in {
            "x86_64": "linux/amd64",
            "aarch64": "linux/arm64",
            "armv7l": "linux/arm/v7",
            "armv6l": "linux/arm/v6",
            "i686": "linux/386",
            "ppc64le": "linux/ppc64le",
            "s390x": "linux/s390x",
        }.items():
            with self.subTest(machine=machine), on_host(machine):
                self.assertEqual(platforms.targets(""), [expected])


class AutomaticArgumentsTest(unittest.TestCase):
    """BuildKit's defaultArgs: eleven arguments."""

    def test_full_set(self) -> None:
        target = platforms.resolve("windows(10.0.17763)/amd64")
        assert target is not None
        with on_host("x86_64"):
            self.assertEqual(
                platforms.automatic_arguments(target, "final"),
                {
                    "BUILDPLATFORM": "linux/amd64",
                    "BUILDOS": "linux",
                    "BUILDOSVERSION": "",
                    "BUILDARCH": "amd64",
                    "BUILDVARIANT": "",
                    "TARGETPLATFORM": "windows(10.0.17763)/amd64",
                    "TARGETOS": "windows",
                    "TARGETOSVERSION": "10.0.17763",
                    "TARGETARCH": "amd64",
                    "TARGETVARIANT": "",
                    "TARGETSTAGE": "final",
                },
            )


class ReferencesTest(unittest.TestCase):
    """FROM lines using automatic arguments resolve per platform."""

    ARCH = "FROM base-${TARGETARCH}\n"

    def test_host_platform_by_default(self) -> None:
        with on_host("x86_64"):
            self.assertEqual(references(self.ARCH, {}).images, ["base-amd64"])

    def test_each_target_platform_contributes(self) -> None:
        found = references(self.ARCH, {}, platforms="linux/amd64,linux/arm64")
        self.assertEqual(found.images, ["base-amd64", "base-arm64"])
        text = "FROM app-${TARGETOS}-${TARGETVARIANT}\n"
        self.assertEqual(
            references(text, {}, platforms="linux/arm").images, ["app-linux-v7"]
        )
        self.assertEqual(
            references(self.ARCH, {}, platforms="amd64").images, ["base-amd64"]
        )

    def test_target_stage(self) -> None:
        # docker build: the target, else the final stage's name, else
        # 'default' when the final stage is unnamed.
        named = "FROM a AS build\nFROM img-${TARGETSTAGE} AS final\n"
        self.assertEqual(references(named, {}).images, ["img-final"])
        unnamed = "FROM a AS build\nFROM img-${TARGETSTAGE}\n"
        self.assertEqual(references(unnamed, {}).images, ["img-default"])
        chosen = "FROM img-${TARGETSTAGE} AS build\nFROM b\n"
        self.assertEqual(references(chosen, {}, "build").images, ["img-build"])

    def test_precedence_matches_docker(self) -> None:
        # docker build: --build-arg TARGETARCH=custom wins even undeclared,
        # and a declared global default beats the automatic value too.
        amd = {"platforms": "linux/amd64"}
        self.assertEqual(
            references(self.ARCH, {"TARGETARCH": "custom"}, **amd).images,
            ["base-custom"],
        )
        declared = "ARG TARGETARCH=custom\n" + self.ARCH
        self.assertEqual(references(declared, {}, **amd).images, ["base-custom"])
        bare = "ARG TARGETARCH\n" + self.ARCH
        self.assertEqual(references(bare, {}, **amd).images, ["base-amd64"])
        self.assertEqual(
            references(declared, {"TARGETARCH": "cli"}, **amd).images, ["base-cli"]
        )

    def test_build_platform_names_a_remote_builder(self) -> None:
        # The implicit target is the whole build platform, OS version
        # included, as BuildKit copies it.
        found = references(
            "FROM img-${TARGETOSVERSION}-${TARGETPLATFORM}\n",
            {},
            build_platform="windows(10.0.17763)/amd64",
        )
        self.assertEqual(found.images, ["img-10.0.17763-windows(10.0.17763)/amd64"])
        # BUILD* describe the builder, not the runner; unset, the runner
        # is assumed, as for the local docker driver.
        text = "FROM tool-${BUILDARCH}\nFROM app-${TARGETARCH}\n"
        with on_host("x86_64"):
            self.assertEqual(references(text, {}).images, ["app-amd64"])
            found = references(text, {}, build_platform="linux/arm64")
            # The builder's platform is also the default target.
            self.assertEqual(found.images, ["app-arm64"])
            both = "FROM tool-${BUILDARCH} AS t\nFROM app-${TARGETARCH}\nCOPY --from=t / /\n"
            found = references(
                both, {}, platforms="linux/amd64", build_platform="linux/arm64"
            )
            self.assertEqual(found.images, ["tool-arm64", "app-amd64"])

    def test_unparsable_platform_is_unresolved(self) -> None:
        found = references(self.ARCH, {}, platforms="linux/amd64,linux/*")
        self.assertEqual(found.images, ["base-amd64"])
        self.assertEqual(found.unresolved, ["platform linux/*"])


class OrderingTest(TreeTestCase):
    """A multi-arch child orders after every per-arch base it can use."""

    def test_child_after_all_platform_bases(self) -> None:
        self.tree(
            {
                "app/Dockerfile": "FROM base-${TARGETARCH}\n",
                "base-amd64/Dockerfile": DOCKERFILE,
                "base-arm64/Dockerfile": DOCKERFILE,
            }
        )
        run = run_action(
            self.workspace, order="dependencies", platforms="linux/amd64,linux/arm64"
        )
        self.assertEqual(run.status, 0, run.annotations)
        entries = json.loads(run.outputs["matrix"])["include"]
        self.assertEqual(
            [e["name"] for e in entries], ["base-amd64", "base-arm64", "app"]
        )
        self.assertEqual(entries[2]["depends_on"], ["base-amd64", "base-arm64"])
        self.assertEqual(
            json.loads(run.outputs["build_levels"]),
            [["base-amd64", "base-arm64"], ["app"]],
        )


if __name__ == "__main__":
    unittest.main()
