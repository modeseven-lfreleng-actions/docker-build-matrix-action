# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Differential tests: the action against the lanes' inline body.

docker-workflows#92 proved the three lanes' copies equivalent with 15
layouts run against each lane configuration, comparing exit status,
images_json, image_count and annotations. docker-workflows#29 names
those cases as this action's acceptance tests. They are ported here
and extended, and every case also compares the build id's
configuration stem, which must match for an unchanged configuration.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

from tests.support import (
    DOCKERFILE,
    jq_keeps_number_literals,
    legacy_available,
    run_action,
    run_legacy,
    workspace,
)

D = DOCKERFILE

# Directory layouts, auto-discovered.
LAYOUTS: dict[str, dict[str, str]] = {
    "root Dockerfile": {"Dockerfile": D},
    "docker/ directory": {"docker/Dockerfile": D, "docker/index.html": "x"},
    "Maven src/main/docker": {"src/main/docker/Dockerfile": D, "pom.xml": "<p/>"},
    "multi-image monorepo": {
        "base-alpine/Dockerfile": D,
        "chain-child/Dockerfile": D,
        "util-echo/Dockerfile": D,
    },
    "root plus subdirectories": {
        "Dockerfile": D,
        "api/Dockerfile": D,
        "web/Dockerfile": D,
    },
    "excluded docker/ and src/": {
        "docker/Dockerfile": D,
        "src/Dockerfile": D,
        "worker/Dockerfile": D,
    },
    "no Dockerfiles at all": {"README.md": "x"},
    "uppercase directory names": {"API/Dockerfile": D, "Web_UI/Dockerfile": D},
    "root and docker/ collide": {"Dockerfile": D, "docker/Dockerfile": D},
    "all three project locations": {
        "Dockerfile": D,
        "docker/Dockerfile": D,
        "src/main/docker/Dockerfile": D,
    },
    "case collision in discovery": {"API/Dockerfile": D, "api/Dockerfile": D},
    "hidden directory ignored": {".hidden/Dockerfile": D, "app/Dockerfile": D},
    "nested beyond depth 1": {"services/api/Dockerfile": D},
    "invalid directory name": {"-bad-/Dockerfile": D},
    "directory without Dockerfile": {"docs/README.md": "x", "app/Dockerfile": D},
}

TREE = {"a/Dockerfile": D, "b/Dockerfile": D, "a/Dockerfile.alt": D}

# Explicit images inputs, run against TREE.
EXPLICIT: dict[str, str] = {
    "declared ordering preserved": '[{"name":"b","context":"b"},{"name":"a","context":"a"}]',
    "build_args and target passthrough": json.dumps(
        [
            {
                "name": "a",
                "context": "a",
                "dockerfile": "a/Dockerfile.alt",
                "target": "t",
                "build_args": ["X=1", "Y=2"],
            }
        ]
    ),
    "extra keys pass through": '[{"context":"a","name":"a","custom":{"k":[1,2]}}]',
    "dockerfile false is absent": '[{"name":"a","context":"a","dockerfile":false,"build_args":null}]',
    "uppercase name normalised": '[{"name":"My_App","context":"a"}]',
    "unicode name normalised": '[{"name":"İmage","context":"a"}]',
    "empty array": "[]",
    "malformed JSON": "{not json",
    "object, not array": '{"name":"a","context":"a"}',
    "missing context": '[{"name":"a"}]',
    "build_args as string": '[{"name":"a","context":"a","build_args":"X=1"}]',
    "numeric dockerfile": '[{"name":"a","context":"a","dockerfile":0}]',
    "non-string build arg": '[{"name":"a","context":"a","build_args":[1]}]',
    "lone surrogate": '[{"name":"a","context":"a","custom":"\\ud800"}]',
    "array element not object": '["a"]',
    "collision after normalisation": '[{"name":"API","context":"a"},{"name":"api","context":"b"}]',
    "invalid name after normalisation": '[{"name":"-x","context":"a"}]',
}

# Lane configurations, as each lane binds its env: block.
CONFIGS: dict[str, dict[str, str]] = {
    "build-test/merge": {},
    "build-test/merge with build_command": {
        "build_command": "make images",
        "build_command_images": "onap/x:1",
    },
    "build-test-release multi-platform": {
        "platforms": "linux/amd64,linux/arm64",
        "ref": "v1.2.3",
    },
    "namespaced Gerrit change": {
        "image_namespace": "onap",
        "gerrit_refspec": "refs/changes/05/146905/3",
    },
}

_BUILD_ID = re.compile(r"[0-9a-f]{12}-[0-9a-f]{16}")


@unittest.skipUnless(legacy_available(), "reference needs bash, jq, sha256sum and od")
class EquivalenceTest(unittest.TestCase):
    """The action matches the inline body across layouts and lanes."""

    def assert_equivalent(self, root: pathlib.Path, **inputs: str) -> None:
        legacy = run_legacy(root, **inputs)
        action = run_action(root, **inputs)
        self.assertEqual(
            action.status, legacy.status, f"status\n{legacy.stdout}\n{action.stdout}"
        )
        self.assertEqual(action.annotations, legacy.annotations)
        for key in ("images_json", "image_count"):
            self.assertEqual(action.outputs.get(key), legacy.outputs.get(key), key)
        if legacy.status == 0:
            self.assertRegex(legacy.outputs["build_id"], _BUILD_ID)
            self.assertRegex(action.outputs["build_id"], _BUILD_ID)
            self.assertEqual(
                action.outputs["build_id"][:12],
                legacy.outputs["build_id"][:12],
                "build id stem",
            )
            self.assertNotEqual(
                action.outputs["build_id"], legacy.outputs["build_id"], "nonce"
            )

    def test_discovered_layouts(self) -> None:
        for layout, files in LAYOUTS.items():
            for config, inputs in CONFIGS.items():
                with (
                    self.subTest(layout=layout, config=config),
                    workspace(files) as root,
                ):
                    self.assert_equivalent(root, **inputs)

    def test_explicit_inputs(self) -> None:
        for case, images in EXPLICIT.items():
            for config, inputs in CONFIGS.items():
                with self.subTest(case=case, config=config), workspace(TREE) as root:
                    self.assert_equivalent(root, images=images, **inputs)

    @unittest.skipUnless(
        jq_keeps_number_literals(), "needs jq 1.7+, which keeps literals"
    )
    def test_extra_values_serialise_as_jq_does(self) -> None:
        # Extra keys pass through, so images_json must carry them in
        # exactly jq's bytes: canonical number literals, DEL escaped.
        numbers = (
            "[1e-7,1E-7,1.0,1.50,100,1e2,1E+2,0.1,-0,-0.0,1e400,"
            "100000000000000000001,12345678901234567890.5,0.000001,"
            "0.0000001,1.5e3,150e1,0e5,5e0,50e-1,123.456e-2]"
        )
        for extra in (numbers, '"a\\u007fb\\u2028c"', '{"z":1e-9,"a":[true,null]}'):
            with self.subTest(extra=extra), workspace(TREE) as root:
                images = f'[{{"name":"a","context":"a","extra":{extra}}}]'
                self.assert_equivalent(root, images=images)

    def test_sub_project_path_prefix(self) -> None:
        files = {
            "sub/proj/Dockerfile": D,
            "sub/proj/extra/Dockerfile": D,
            "other/Dockerfile": D,
        }
        for config, inputs in CONFIGS.items():
            with self.subTest(config=config), workspace(files) as root:
                self.assert_equivalent(root, path_prefix="sub/proj", **inputs)


if __name__ == "__main__":
    unittest.main()
