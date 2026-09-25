#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Reference implementation for differential tests. DO NOT EDIT.
#
# The body below stays verbatim, so lint exceptions live in this header.
# SC2164: the runner's bash -e already stops on a failed cd.
# shellcheck disable=SC2164
#
# The "Discover images" step body from lfreleng-actions/docker-workflows
# at v0.6.2 (f514775e0d7ac4789fddf066b4d5a02c5dc038d8), where #92
# converged it: build-test.yaml, merge.yaml and build-test-release.yaml
# carry this body byte for byte, differing only in the env: values they
# bind. Extracted by removing the ten-space YAML indent and trailing
# whitespace; the tests run it as the runner does, with bash -eo pipefail.
#
# tests/test_equivalence.py runs it and the action side by side and
# compares exit status, images_json, image_count, annotations and the
# build id configuration stem.

# Resolve the image list: an explicit 'images' input wins;
# otherwise walk the estate-observed Dockerfile locations.
cd "${PATH_PREFIX}"
if [ -n "${IMAGES_INPUT}" ]; then
  if ! jq -e 'type == "array" and length > 0 and
      all(.[]; (.name | type == "string") and
               (.context | type == "string") and
               ((.dockerfile // "") | type == "string") and
               ((.target // "") | type == "string") and
               ((.build_args // []) | type == "array" and
                 all(.[]; type == "string")))' \
      >/dev/null 2>&1 <<< "${IMAGES_INPUT}"; then
    echo "::error::images input must be a non-empty JSON" \
      "array of objects with string 'name' and 'context'" \
      "keys; optional 'dockerfile'/'target' take strings" \
      "and 'build_args' a list of KEY=VALUE strings. Pass" \
      "an empty string (not []) for auto-discovery"
    exit 1
  fi
  images=$(jq -c . <<< "${IMAGES_INPUT}")
else
  repo_name="${TARGET_REPOSITORY##*/}"
  if [ "${PATH_PREFIX}" != "." ]; then
    repo_name=$(basename "${PATH_PREFIX}")
  fi
  images='[]'
  add_image() {
    images=$(jq -c \
      --arg n "$1" --arg d "$2" --arg c "$3" \
      '. + [{name: $n, dockerfile: $d, context: $c}]' \
      <<< "${images}")
  }
  if [ -f Dockerfile ]; then
    add_image "${repo_name}" "Dockerfile" "."
  fi
  if [ -f docker/Dockerfile ]; then
    add_image "${repo_name}" "docker/Dockerfile" "docker"
  fi
  if [ -f src/main/docker/Dockerfile ]; then
    add_image "${repo_name}" \
      "src/main/docker/Dockerfile" "src/main/docker"
  fi
  for df in */Dockerfile; do
    [ -f "${df}" ] || continue
    dir="${df%/Dockerfile}"
    case "${dir}" in
      docker|src) continue ;;
    esac
    add_image "${dir}" "${df}" "${dir}"
  done
fi
# Sanitise names to the docker repository character set
images=$(jq -c \
  '[.[] | .name |= (ascii_downcase |
    gsub("[^a-z0-9._-]"; "-"))]' \
  <<< "${images}")
if [ -n "${IMAGES_INPUT}" ]; then
  # Explicit entries whose names collide after
  # normalisation (e.g. API and api) must reject: silently
  # dropping one would build only part of the caller's
  # requested image set, and the publish lanes reject the
  # same input — verify must not pass what merge refuses
  dupes=$(jq -r '[.[].name] | group_by(.) |
    map(select(length > 1) | .[0]) | .[]' \
    <<< "${images}")
  if [ -n "${dupes}" ]; then
    echo "::error::Duplicate image name(s) after" \
      "normalisation in the images input: ${dupes}"
    exit 1
  fi
else
  # Auto-discovery drops duplicates (e.g. a root Dockerfile
  # plus docker/ both mapping to the repository name). The
  # de-duplication preserves array order — order is the
  # build order, which same-repository FROM chains rely on
  # — keeping the first occurrence of each name.
  images=$(jq -c \
    'reduce .[] as $img ([];
      if any(.[]; .name == $img.name) then .
      else . + [$img] end)' \
    <<< "${images}")
fi
count=$(jq 'length' <<< "${images}")
if [ "${count}" -eq 0 ] && [ -z "${BUILD_COMMAND}" ]; then
  echo "::error::No Dockerfiles found under" \
    "'${PATH_PREFIX}' and no images input provided"
  exit 1
fi
if [ "${count}" -eq 0 ]; then
  # Reached in the lanes offering the build_command escape
  # hatch, when the caller supplied one. Project tooling
  # such as jib or a Gradle plugin synthesises an image
  # with no Dockerfile to find, so an empty discovery
  # result is not an error there; the build job enumerates
  # whatever the command created. A lane without the hatch
  # passes an empty BUILD_COMMAND and has already exited
  # above.
  echo "::notice::No Dockerfiles discovered under" \
    "'${PATH_PREFIX}'; build_command builds the images"
fi
# Post-sanitisation validation: every name must be a valid
# Docker repository component (start and end alphanumeric),
# so tag construction fails here with a clear message rather
# than at build time.
bad=$(jq -r '.[] | .name |
  select(test("^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$") | not)' \
  <<< "${images}")
if [ -n "${bad}" ]; then
  echo "::error::Invalid image name(s) after sanitisation:" \
    "${bad} (names must start/end with a-z or 0-9)"
  exit 1
fi
echo "Discovered ${count} image(s):"
jq -r '.[] | "  \(.name)  (\(.dockerfile //
  (.context + "/Dockerfile")))"' <<< "${images}"
echo "images_json=${images}" >> "$GITHUB_OUTPUT"
echo "image_count=${count}" >> "$GITHUB_OUTPUT"
# Artifact names must be unique per workflow-call invocation:
# matrix legs calling this workflow in parallel would
# otherwise collide on the docker-archives/sbom-files names.
# Duplicates are not rejected or merged: each upload is kept
# as its own artifact and download-artifact resolves a name
# to the newest match, so a downstream job would silently
# consume another leg's images.
#
# The id therefore has two parts. The first is a hash of the
# inputs that decide what gets built, including the checkout
# identity: two invocations of one repository can differ only
# by the ref they resolve, and omitting it collided a
# build-test leg with a merge dry run in this repository's own
# self-test. Every lane hashes the same field set so this
# block stays identical across them, and a lane lacking an
# input passes it empty.
#
# A hash of inputs cannot distinguish two invocations whose
# inputs are identical, though, and nothing stops a caller
# making that call twice. The second part is therefore a
# nonce. It is drawn once here and reaches the other jobs
# through this job's outputs, so it is constant within an
# invocation and distinct between them, and no caller has to
# remember to pass a discriminator. Keeping the hash as a
# prefix preserves the diagnostic value: identical
# configurations still share a recognisable stem.
#
# 64 bits, because a nonce is only probabilistically unique
# and the width decides whether that distinction matters.
# Across n invocations in a run the chance of a repeat is
# about n^2/2 over the space: at 32 bits a thousand-leg run
# sits near 1e-4, which is small but not negligible once
# multiplied by every run forever. At 64 bits the same case
# is near 3e-14, below the rate at which the surrounding
# machinery fails for other reasons.
#
# The id names artifacts within a run and never keys a cache
# across runs, so it does not need to be reproducible.
config_id=$(printf '%s|%s|%s|%s|%s|%s|%s|%s|%s' \
  "${TARGET_REPOSITORY}" "${REF}" "${GERRIT_REFSPEC}" \
  "${PATH_PREFIX}" "${IMAGES_INPUT}" \
  "${IMAGE_NAMESPACE}" "${BUILD_COMMAND}" \
  "${BUILD_COMMAND_IMAGES}" "${PLATFORMS}" \
  | sha256sum | cut -c1-12)
nonce=$(od -An -N8 -tx8 /dev/urandom | tr -d ' \n')
build_id="${config_id}-${nonce}"
echo "build_id=${build_id}" >> "$GITHUB_OUTPUT"
{
  echo "## Docker Images"
  echo ""
  echo "| Image | Dockerfile | Context |"
  echo "| ----- | ---------- | ------- |"
  jq -r '.[] | "| \(.name) | \(.dockerfile //
    (.context + "/Dockerfile")) | \(.context) |"' \
    <<< "${images}"
  echo ""
} >> "$GITHUB_STEP_SUMMARY"
