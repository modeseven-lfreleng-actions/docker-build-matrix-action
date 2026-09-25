# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""BuildKit's automatic arguments, from containerd's platform rules.

BuildKit (dockerfile2llb defaultArgs) puts BUILDPLATFORM, BUILDOS,
BUILDOSVERSION, BUILDARCH, BUILDVARIANT, the matching five TARGET*
arguments and TARGETSTAGE in the global scope, so ``FROM
base-${TARGETARCH}`` resolves without an ARG declaration.

Platform specifiers are read as containerd's platforms.Parse reads
them and then normalised as platforms.Normalize does; this module is
a port of both (github.com/containerd/platforms, database.go and
platforms.go). docker build confirms the results, for example
'linux/arm' giving TARGETPLATFORM 'linux/arm/v7' and 'linux/arm64/v8'
giving 'linux/arm64'.
"""

from __future__ import annotations

import platform as _host
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import unquote

# isKnownOS and isKnownArch, generated upstream from Go's syslist.go.
_KNOWN_OS = frozenset(
    "aix android darwin dragonfly freebsd hurd illumos ios js linux nacl "
    "netbsd openbsd plan9 solaris windows zos".split()
)
_KNOWN_ARCH = frozenset(
    "386 amd64 amd64p32 arm armbe arm64 arm64be ppc64 ppc64le loong64 mips "
    "mipsle mips64 mips64le mips64p32 mips64p32le ppc riscv riscv64 s390 "
    "s390x sparc sparc64 wasm".split()
)
_SPECIFIER = re.compile(r"[A-Za-z0-9_.-]+")
_OS = re.compile(
    r"([A-Za-z0-9_-]+)(?:\(([A-Za-z0-9_.%-]*)((?:\+[A-Za-z0-9_.%-]+)*)\))?"
)
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")

# How `uname -m` names the host, which Go's runtime.GOARCH (and the
# variant containerd detects) would report instead.
_UNAME = {
    "x86_64": ("amd64", ""),
    "amd64": ("amd64", ""),
    "aarch64": ("arm64", ""),
    "arm64": ("arm64", ""),
    "arm64e": ("arm64", ""),
    "i386": ("386", ""),
    "i486": ("386", ""),
    "i586": ("386", ""),
    "i686": ("386", ""),
    "armv5l": ("arm", "v5"),
    "armv6l": ("arm", "v6"),
    "armv7l": ("arm", "v7"),
    "armv8l": ("arm", "v8"),
    "ppc64el": ("ppc64le", ""),
}


@dataclass(frozen=True)
class Platform:
    """An OCI platform: os, architecture, variant and OS version."""

    os: str
    arch: str
    variant: str = ""
    os_version: str = ""
    os_features: tuple[str, ...] = ()

    def format(self) -> str:
        """platforms.Format: os/arch[/variant]."""
        return "/".join(part for part in (self.os, self.arch, self.variant) if part)

    def format_all(self) -> str:
        """platforms.FormatAll: Format with any OS version and features.

        Features are sorted, with empty and duplicate ones skipped, as
        formatOSFeatures writes them.
        """
        features = "+".join(_encode(f) for f in sorted(set(self.os_features)) if f)
        options = _encode(self.os_version) + (f"+{features}" if features else "")
        head = f"{self.os}({options})" if options else self.os
        return "/".join(part for part in (head, self.arch, self.variant) if part)


def _encode(value: str) -> str:
    """containerd's osOptionReplacer: only the characters its own
    syntax uses are escaped, '%' first so nothing double-encodes."""
    for char, escaped in (
        ("%", "%25"),
        ("+", "%2B"),
        ("(", "%28"),
        (")", "%29"),
        ("/", "%2F"),
    ):
        value = value.replace(char, escaped)
    return value


def normalize_os(os_name: str) -> str:
    """containerd's normalizeOS."""
    os_name = os_name.lower()
    if not os_name:
        return "linux"  # runtime.GOOS: Docker builds these on Linux
    return "darwin" if os_name == "macos" else os_name


def normalize_arch(arch: str, variant: str) -> tuple[str, str]:
    """containerd's normalizeArch, case for case."""
    arch, variant = arch.lower(), variant.lower()
    if arch == "i386":
        return "386", ""
    if arch in ("x86_64", "x86-64", "amd64"):
        return "amd64", "" if variant == "v1" else variant
    if arch in ("aarch64", "arm64"):
        if variant in ("8", "v8", "v8.0"):
            variant = ""
        elif variant in ("9", "9.0", "v9.0"):
            variant = "v9"
        return "arm64", variant
    if arch == "armhf":
        return "arm", "v7"
    if arch == "armel":
        return "arm", "v6"
    if arch == "arm":
        if variant in ("", "7"):
            variant = "v7"
        elif variant in ("5", "6", "8"):
            variant = "v" + variant
    return arch, variant


def host() -> Platform:
    """The build host, as runtime.GOOS/GOARCH and cpuVariant give it."""
    machine = _host.machine().lower()
    arch, variant = _UNAME.get(machine, (machine, ""))
    return Platform("linux", arch, variant)


def parse(spec: str) -> Platform | None:
    """containerd's platforms.Parse, or None where it would fail."""
    if "*" in spec:
        return None
    parts = spec.split("/", 3)
    found = _OS.fullmatch(parts[0])
    if not found or not all(_SPECIFIER.fullmatch(part) for part in parts[1:]):
        return None
    os_name = normalize_os(found.group(1))
    # Go's url.PathUnescape fails on a '%' without two hex digits after
    # it; urllib's unquote would pass it through, accepting a spec
    # containerd and buildx reject.
    options = (found.group(2) or "") + (found.group(3) or "")
    if _BAD_ESCAPE.search(options):
        return None
    os_version = unquote(found.group(2) or "")
    features = tuple(unquote(f) for f in (found.group(3) or "").split("+")[1:])
    if len(parts) == 1:
        if os_name in _KNOWN_OS:
            native = host()
            variant = (
                native.variant
                if native.arch == "arm" and native.variant != "v7"
                else ""
            )
            return Platform(os_name, native.arch, variant, os_version, features)
        arch, variant = normalize_arch(parts[0], "")
        if arch == "arm" and variant == "v7":
            variant = ""
        if arch in _KNOWN_ARCH:
            return Platform("linux", arch, variant)
        return None
    if len(parts) == 2:
        arch, variant = normalize_arch(parts[1], "")
        if arch == "arm" and variant == "v7":
            variant = ""
        return Platform(os_name, arch, variant, os_version, features)
    if len(parts) == 3:
        arch, variant = normalize_arch(parts[1], parts[2])
        if arch == "arm64" and not variant:
            variant = "v8"
        return Platform(os_name, arch, variant, os_version, features)
    return None


def normalize(platform: Platform) -> Platform:
    """containerd's platforms.Normalize."""
    arch, variant = normalize_arch(platform.arch, platform.variant)
    return Platform(
        normalize_os(platform.os),
        arch,
        variant,
        platform.os_version,
        tuple(sorted(set(platform.os_features))),
    )


def resolve(spec: str) -> Platform | None:
    """A specifier as BuildKit uses it: parsed, then normalised."""
    parsed = parse(spec)
    return normalize(parsed) if parsed else None


def automatic_arguments(
    target: Platform, stage: str, builder: Platform | None = None
) -> dict[str, str]:
    """BuildKit's defaultArgs for a build of ``stage`` for ``target``.

    ``builder`` is BuildKit's build platform. Unset, it is the runner,
    which is right for the local docker driver the lanes use; a remote
    or cross-architecture builder must be named by the caller, since
    nothing on the runner can tell what it is.
    """
    build = normalize(builder or host())
    return {
        "BUILDPLATFORM": build.format(),
        "BUILDOS": build.os,
        "BUILDOSVERSION": build.os_version,
        "BUILDARCH": build.arch,
        "BUILDVARIANT": build.variant,
        "TARGETPLATFORM": target.format_all(),
        "TARGETOS": target.os,
        "TARGETOSVERSION": target.os_version,
        "TARGETARCH": target.arch,
        "TARGETVARIANT": target.variant,
        "TARGETSTAGE": stage,
    }


def targets(platforms: str, builder: Platform | None = None) -> Sequence[str]:
    """Comma-separated platforms, or the builder's when there are none.

    BuildKit targets its build platform by default, OS version and
    features included, so format_all() carries all of it through; that
    is the runner unless ``builder`` says otherwise.
    """
    listed = [item.strip() for item in platforms.split(",") if item.strip()]
    return listed or [normalize(builder or host()).format_all()]
