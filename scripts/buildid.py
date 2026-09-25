# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The per-invocation build id that names a run's artifacts.

Artifact names must be unique per invocation. Duplicates are neither
rejected nor merged: each upload is kept, and download-artifact
resolves a name to the newest match, so a downstream job would
silently consume another invocation's images (docker-workflows#92).

The id therefore has two parts:

* a hash of the inputs that decide what gets built, including the
  checkout identity, so identical configurations share a
  recognisable stem; and
* a 64-bit nonce, because no hash can separate two invocations with
  identical inputs. Across n invocations the chance of a repeat is
  about n^2/2 over the space: near 3e-14 for a thousand-leg run.

The id names artifacts within a run and never keys a cache across
runs, so it does not need to be reproducible.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence


def config_hash(fields: Sequence[str]) -> str:
    """First 12 hex digits of SHA-256 over the '|'-joined fields.

    Byte-compatible with ``printf '%s|%s…' … | sha256sum | cut -c1-12``,
    so the stem is unchanged for callers migrating from the inline
    implementation. surrogateescape round-trips any non-UTF-8 bytes
    the environment carried.
    """
    joined = "|".join(fields).encode("utf-8", "surrogateescape")
    return hashlib.sha256(joined).hexdigest()[:12]


def build_id(fields: Sequence[str]) -> str:
    """``<config hash>-<16 hex digit nonce>``."""
    return f"{config_hash(fields)}-{secrets.token_hex(8)}"
