# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Shared helpers: scratch trees and running either implementation."""

from __future__ import annotations

import contextlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEGACY = ROOT / "tests" / "legacy" / "discover-v0.6.2.sh"

# Lane input name -> the variable the legacy step body reads.
LEGACY_ENV = {
    "path_prefix": "PATH_PREFIX",
    "images": "IMAGES_INPUT",
    "repository": "TARGET_REPOSITORY",
    "ref": "REF",
    "gerrit_refspec": "GERRIT_REFSPEC",
    "image_namespace": "IMAGE_NAMESPACE",
    "build_command": "BUILD_COMMAND",
    "build_command_images": "BUILD_COMMAND_IMAGES",
    "platforms": "PLATFORMS",
}

LANE_DEFAULTS = {
    "path_prefix": ".",
    "images": "",
    "repository": "example-org/example-repo",
    "ref": "refs/heads/main",
    "gerrit_refspec": "",
    "image_namespace": "",
    "build_command": "",
    "build_command_images": "",
    "platforms": "",
}

DOCKERFILE = "FROM busybox:1.37\n"


@dataclass
class Run:
    """The observable result of one invocation."""

    status: int
    outputs: dict[str, str]
    annotations: list[str]
    stdout: str
    summary: str


def write_tree(root: pathlib.Path, files: Mapping[str, str]) -> None:
    """Create ``files`` (relative path -> content) under ``root``."""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def parse_outputs(text: str) -> dict[str, str]:
    """Parse GITHUB_OUTPUT in both the ``k=v`` and heredoc forms."""
    outputs: dict[str, str] = {}
    # The runner reads GITHUB_OUTPUT line by line on LF (trimming a CR);
    # splitlines() would also split on U+2028 and friends.
    lines = iter(line.removesuffix("\r") for line in text.split("\n"))
    for line in lines:
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delimiter = line.split("<<", 1)
            body: list[str] = []
            for item in lines:
                if item == delimiter:
                    break
                body.append(item)
            outputs[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
    return outputs


def invoke(command: list[str], workspace: pathlib.Path, env: Mapping[str, str]) -> Run:
    with tempfile.TemporaryDirectory() as scratch:
        output = pathlib.Path(scratch, "output")
        summary = pathlib.Path(scratch, "summary")
        output.touch()
        summary.touch()
        full_env = {
            "PATH": os.environ.get("PATH", ""),
            # Fix collation so the shell glob sorts as the runner's
            # C.UTF-8 locale does, whatever the developer's locale.
            "LC_ALL": "C",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "PYTHONPATH": str(ROOT),
            **env,
        }
        proc = subprocess.run(
            command,
            cwd=workspace,
            env=full_env,
            capture_output=True,
            text=True,
            check=False,
        )
        return Run(
            status=proc.returncode,
            outputs=parse_outputs(output.read_text(encoding="utf-8")),
            annotations=[
                line for line in proc.stdout.splitlines() if line.startswith("::")
            ],
            stdout=proc.stdout + proc.stderr,
            summary=summary.read_text(encoding="utf-8"),
        )


def run_action(workspace: pathlib.Path, **inputs: str) -> Run:
    """Run the action's entry point exactly as action.yaml does."""
    env = {
        f"INPUT_{key.upper()}": value
        for key, value in {**LANE_DEFAULTS, **inputs}.items()
    }
    return invoke([sys.executable, "-I", str(ROOT / "entrypoint.py")], workspace, env)


def run_legacy(workspace: pathlib.Path, **inputs: str) -> Run:
    """Run the extracted docker-workflows step body, as the runner does."""
    merged = {**LANE_DEFAULTS, **inputs}
    env = {LEGACY_ENV[key]: value for key, value in merged.items() if key in LEGACY_ENV}
    return invoke(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(LEGACY)],
        workspace,
        env,
    )


def legacy_available() -> bool:
    """The reference needs bash, jq and sha256sum, as the runner has."""
    return all(shutil.which(tool) for tool in ("bash", "jq", "sha256sum", "od"))


def jq_keeps_number_literals() -> bool:
    """jq 1.7+, as on ubuntu-latest, keeps literals; 1.6 re-renders them."""
    if not shutil.which("jq"):
        return False
    proc = subprocess.run(
        ["jq", "-c", "."],
        input="[1.50,1e-7]",
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() == "[1.50,1E-7]"


@contextlib.contextmanager
def workspace(files: Mapping[str, str]) -> Iterator[pathlib.Path]:
    """A scratch workspace holding ``files``, removed on exit."""
    with tempfile.TemporaryDirectory() as scratch:
        root = pathlib.Path(scratch)
        write_tree(root, files)
        yield root


class TreeTestCase(unittest.TestCase):
    """A test case with a fresh scratch workspace per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def tree(self, files: Mapping[str, str]) -> pathlib.Path:
        """Populate the workspace and return it."""
        write_tree(self.workspace, files)
        return self.workspace
