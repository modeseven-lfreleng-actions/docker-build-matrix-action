<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 🐳 Docker Build Matrix Action

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/docker-build-matrix-action) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![pre-commit.ci status badge]][pre-commit.ci results page] [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/docker-build-matrix-action/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/docker-build-matrix-action)
<!-- prettier-ignore-end -->

Works out which container images a repository builds, and in what
order, then publishes the answer in three shapes: an ordered build
list, a `strategy.matrix` and a set of parallel build stages.

It builds nothing itself. Building belongs to the caller, whether that
is a shell loop, `docker/build-push-action` in a matrix job, or the
[docker-workflows] lanes.

## docker-build-matrix-action

The same action serves a one-image repository and a monorepo:

- **Simple repositories** need no inputs. The action finds the
  Dockerfile and names the image after the repository.
- **Monorepos** can walk nested directories, exclude paths, build a
  subset of images, and order builds so that a base image built in the
  same repository comes before the images that use it.

Every default reproduces the image discovery that the three
[docker-workflows] lanes carried inline up to v0.6.2
([docker-workflows#29]), so a lane can swap its "Discover images" step
for this action with no change in behaviour. The test suite proves
that against the original step body; see
[Compatibility](#compatibility).

## Usage Example

### Single-image repository

<!-- markdownlint-disable MD046 -->

```yaml
steps:
  - uses: actions/checkout@<sha>  # vX.Y.Z
  - name: "Discover images"
    id: images
    uses: lfreleng-actions/docker-build-matrix-action@<sha>  # vX.Y.Z

  - name: "Build images"
    shell: bash
    env:
      IMAGES: ${{ steps.images.outputs.images_json }}
    run: |
      jq -c '.[]' <<< "${IMAGES}" | while read -r image; do
        name=$(jq -r .name <<< "${image}")
        context=$(jq -r .context <<< "${image}")
        dockerfile=$(jq -r '.dockerfile // (.context + "/Dockerfile")' \
          <<< "${image}")
        docker build -t "${name}:verify" -f "${dockerfile}" "${context}"
      done
```

### Monorepo with same-repository base images

`order: dependencies` reads each Dockerfile's `FROM`, `COPY --from`
and `RUN --mount` references and builds sibling base images first.
The action resolves global `ARG` defaults and each image's
`build_args`, so this common chain idiom builds base first:

```dockerfile
ARG BASE_IMAGE=base-alpine:verify
FROM ${BASE_IMAGE}
```

```yaml
- name: "Discover images"
  id: images
  uses: lfreleng-actions/docker-build-matrix-action@<sha>  # vX.Y.Z
  with:
    search_depth: '2'           # also find services/<name>/Dockerfile
    name_from: path             # services/api -> services-api
    exclude_paths: 'tests/*'
    order: dependencies
    image_namespace: onap
```

### One matrix job per image

The `matrix` output feeds `strategy.matrix` directly. GitHub rejects
an empty matrix, so guard the job on `image_count`:

```yaml
jobs:
  discover:
    runs-on: ubuntu-latest
    outputs:
      matrix: ${{ steps.images.outputs.matrix }}
      count: ${{ steps.images.outputs.image_count }}
    steps:
      - uses: actions/checkout@<sha>  # vX.Y.Z
      - id: images
        uses: lfreleng-actions/docker-build-matrix-action@<sha>  # vX.Y.Z

  build:
    needs: discover
    if: needs.discover.outputs.count != '0'
    runs-on: ubuntu-latest
    strategy:
      matrix: ${{ fromJSON(needs.discover.outputs.matrix) }}
    steps:
      - uses: actions/checkout@<sha>  # vX.Y.Z
      - uses: docker/build-push-action@<sha>  # vX.Y.Z
        with:
          context: ${{ matrix.context_path }}
          file: ${{ matrix.dockerfile_path }}
          target: ${{ matrix.target }}
          platforms: ${{ matrix.platforms }}
          tags: ${{ matrix.image }}:${{ github.sha }}
```

Parallel matrix legs cannot see one another's images. When images
build from siblings, build `build_levels` stage by stage instead, or
build in `images_json` order in one job.

### Building a subset

`select` picks images by name glob, for example from a changed-files
step. With `select_dependencies` (the default) the action adds the
same-repository images they build from, so the chain still resolves:

```yaml
- uses: lfreleng-actions/docker-build-matrix-action@<sha>  # vX.Y.Z
  with:
    select: ${{ steps.changes.outputs.images }}
    allow_empty: 'true'         # no changed image is not an error
```

### Replacing the inline step in a docker-workflows lane

Inputs take the lanes' names, so the step's `env:` block maps across
one for one, and the outputs keep the job output names:

```yaml
- name: 'Discover images'
  id: discover
  uses: lfreleng-actions/docker-build-matrix-action@<sha>  # vX.Y.Z
  with:
    path_prefix: ${{ inputs.path_prefix }}
    images: ${{ inputs.images }}
    repository: ${{ inputs.repository || github.repository }}
    # The ref this job's checkout step resolved, which differs by
    # lane: inputs.ref, gerrit_revision || ref, or the release tag.
    ref: ${{ inputs.ref }}
    gerrit_refspec: ${{ inputs.gerrit_refspec }}
    image_namespace: ${{ inputs.image_namespace }}
    build_command: ${{ inputs.build_command }}
    build_command_images: ${{ inputs.build_command_images }}
```

<!-- markdownlint-enable MD046 -->

## Inputs

<!-- markdownlint-disable MD013 -->

| Name                   | Required | Default                 | Description                                                                                                                   |
| ---------------------- | -------- | ----------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `path_prefix`          | False    | `.`                     | Project root, relative to the workspace. Contexts and Dockerfiles are relative to it                                          |
| `images`               | False    | `''`                    | Explicit JSON image list ([schema](#image-entries)); empty auto-discovers                                                     |
| `image_namespace`      | False    | `''`                    | Namespace for the matrix `image` field, for example `onap`                                                                    |
| `platforms`            | False    | `''`                    | Default platforms for matrix entries without their own                                                                        |
| `build_platform`       | False    | `''`                    | The builder's platform, for BuildKit's `BUILD*` arguments; empty is the runner's. Set it for a remote builder                 |
| `build_command`        | False    | `''`                    | The caller's image-producing command. Not run here: when set, finding no Dockerfile is not an error                           |
| `build_command_images` | False    | `''`                    | Images that command produces; the action records them in the build id and nowhere else                                        |
| `repository`           | False    | `github.repository`     | `owner/name`; names project-level images and feeds the build id                                                               |
| `ref`                  | False    | `''`                    | The ref the calling job checked out, for the build id                                                                         |
| `gerrit_refspec`       | False    | `''`                    | Gerrit change refspec, for the build id                                                                                       |
| `search_depth`         | False    | `1`                     | Directory levels searched for per-image Dockerfiles (1 to 32)                                                                 |
| `name_from`            | False    | `directory`             | Name discovered images by final `directory`, or by relative `path` joined with `-`                                            |
| `exclude_paths`        | False    | `''`                    | Comma or newline separated directory globs that discovery skips, with their subtrees                                          |
| `order`                | False    | `declared`              | `declared` keeps discovery or input order; `dependencies` builds same-repository bases first                                  |
| `select`               | False    | `''`                    | Comma or space separated name globs choosing a subset                                                                         |
| `select_dependencies`  | False    | `true`                  | With `select`, add the same-repository images the selection builds from                                                       |
| `allow_empty`          | False    | `false`                 | Succeed with no images instead of failing                                                                                     |
| `summary`              | False    | `true`                  | Write the image table to the step summary                                                                                     |

<!-- markdownlint-enable MD013 -->

## Outputs

<!-- markdownlint-disable MD013 -->

| Name           | Description                                                                                              |
| -------------- | -------------------------------------------------------------------------------------------------------- |
| `images_json`  | Ordered image list as compact JSON, in the input schema; the docker-workflows build loop contract        |
| `image_count`  | Number of images                                                                                         |
| `image_names`  | Space-separated names, in build order                                                                    |
| `matrix`       | `{"include": [...]}` for `strategy.matrix`, one [entry](#matrix-entries) per image                       |
| `build_levels` | JSON array of stages; images within a stage do not depend on one another                                 |
| `build_id`     | Unique per invocation: 12 hex digits of configuration hash, `-`, 16 hex digits of nonce                  |
| `source`       | `explicit` when the `images` input applied, else `discovered`                                            |

<!-- markdownlint-enable MD013 -->

## Implementation Details

### Image entries

The `images` input and the `images_json` output share one schema:

<!-- markdownlint-disable MD013 -->

| Key          | Type                        | Required | Meaning                                                                                                              |
| ------------ | --------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------- |
| `name`       | string                      | Yes      | Image name; lowercased, other characters become `-`                                                                  |
| `context`    | string                      | Yes      | Build context, relative to `path_prefix`                                                                             |
| `dockerfile` | string                      | No       | Dockerfile relative to `path_prefix`; default `<context>/Dockerfile`                                                 |
| `target`     | string                      | No       | Build stage to stop at                                                                                               |
| `build_args` | array of strings            | No       | `KEY=VALUE` build arguments                                                                                          |
| `platforms`  | string, or array of strings | No       | Platforms for this image; the matrix joins an array with commas, and falls back to the `platforms` input when absent |

<!-- markdownlint-enable MD013 -->

The action rejects names that remain invalid after normalisation, and
explicit entries whose names collide after it (`API` and `api`), since
dropping one without a word would build part of the requested set.
Other keys pass through to `images_json` and the matrix unchanged, so
callers can attach per-image settings for their own jobs to read.

### Matrix entries

Each matrix entry carries the image entry's keys, normalised, plus:

<!-- markdownlint-disable MD013 -->

| Key               | Meaning                                                                            |
| ----------------- | ---------------------------------------------------------------------------------- |
| `dockerfile`      | Always present, defaulted                                                          |
| `target`          | Always present; empty when unset                                                   |
| `build_args`      | Always present; empty list when unset                                              |
| `platforms`       | The entry's own `platforms` (a list joins with commas), else the `platforms` input |
| `image`           | `<image_namespace>/<name>`, or `<name>` without a namespace                        |
| `context_path`    | Context relative to the workspace, for `docker/build-push-action`                  |
| `dockerfile_path` | Dockerfile relative to the workspace                                               |
| `depends_on`      | Same-repository images this one builds from, when the action read them             |
| `level`           | Index of this image's stage in `build_levels`                                      |
| `index`           | Position in build order                                                            |

<!-- markdownlint-enable MD013 -->

### Discovery

With `images` empty, the action walks `path_prefix` in this order:

1. `Dockerfile`, then `docker/Dockerfile`, then
   `src/main/docker/Dockerfile` (the Maven convention). These take the
   project name: the last `path_prefix` directory, or else the
   repository name.
2. `<dir>/Dockerfile` for each directory in code point order, named
   after the directory. At the top level `docker/` and `src/` are
   never images themselves. Hidden directories never qualify.

`search_depth` extends step 2 to nested directories, visited in sorted
path order. The walk follows a symbolic link to a directory to check
it for a Dockerfile, but never descends through one, so a link cannot
loop.

`exclude_paths` globs match a directory or any directory above it, so
excluding `src` also drops `src/main/docker/Dockerfile`. The pattern
`.` names the root context alone, dropping the root `Dockerfile`.

Where two discovered images share a name, the first wins and the log
records the one skipped. The common case is a root `Dockerfile` beside
`docker/Dockerfile`. `name_from: path` gives nested images distinct
names, and fails rather than skip an image in the rare layouts where
two directories still map to one name: `a/b` beside `a-b`, or names
that differ in characters sanitising replaces.

Order is build order, and discovery never reorders beyond the walk
above. A layout whose chain needs a different order either lists its
images explicitly or uses `order: dependencies`.

### Dependency ordering

With `order: dependencies`, a reference names a sibling when it
could resolve to an image this build produces, and not otherwise. As
the lanes tag them, that means:
unqualified (`base:verify`, which Docker also spells
`docker.io/library/base`), or under `image_namespace` on any registry
(`onap/base:1.2` or `nexus3.onap.org:10001/onap/base:1.2` with
namespace `onap`). A third party's image that shares a sibling's
name, such as `ghcr.io/vendor/base`, is not a sibling: a false edge
is not harmless, since with a real edge the other way it makes a
cycle and fails the run.

The sort is stable: among images whose bases have all built, the
earliest declared goes first, so a list already in a valid order
keeps that order. A cycle fails, naming its members apart from the
images it blocks.

As in BuildKit, the stages a build reaches are the ones that count:
those the image's `target` depends on or, without a `target`, those
the final stage depends on. A `FROM` in a stage the build never runs
cannot order it, or invent a cycle.

BuildKit's automatic arguments (`TARGETPLATFORM`, `TARGETARCH`,
`TARGETSTAGE`, `BUILDPLATFORM` and the rest) are in scope too, set
for each platform the image builds for: its own `platforms`, else
the `platforms` input, else the builder's (`build_platform`, which
defaults to the runner's, right for the local docker driver).
Platforms parse and
normalise as containerd does, and `TARGETSTAGE` is the `target`, else
the final stage's name, else `default`. An image building
`FROM base-${TARGETARCH}` for
`linux/amd64,linux/arm64` follows both `base-amd64` and
`base-arm64`. As in `docker build`, a declared global default or a
`build_args` entry overrides an automatic value.

The action does not guess what it cannot resolve. As in BuildKit, an
unset `ARG` expands to an empty string, so `FROM base${SUFFIX}` still
builds from `base`. A reference that expands to nothing, or uses a
form BuildKit would reject, raises a warning and adds no edge, as does
a Dockerfile it cannot read. Stage aliases, numeric `--from` indices
and `scratch` never count as images.

The parser reads lines as BuildKit does: the `escape` parser directive
selects the continuation character, comment and blank lines drop out
of continuations, and heredoc bodies stay hidden, including those that
`ONBUILD` opens. As in `docker build`, `RUN << EOF` opens a heredoc,
while a shell here-string (`<<<`) and the JSON form never do.

### Build id

`build_id` names a run's artifacts. Artifact names must be unique per
invocation: download-artifact resolves a duplicated name to the newest
upload, so a job could test another invocation's images unnoticed
([docker-workflows#92]).

The configuration hash covers the inputs that decide what gets built,
including the checkout identity. For the lanes' inputs it equals the
hash the inline step computed, and a tunable joins it when set to a
non-default value. The nonce separates invocations whose inputs are
identical. The id names a run's artifacts and never keys a cache, so
reproducibility is not a goal.

## Compatibility

`tests/legacy/discover-v0.6.2.sh` is the lanes' step body, extracted
verbatim from docker-workflows v0.6.2, where #92 converged the three
copies. `tests/test_equivalence.py` runs it and this action side by
side over the fifteen layouts #92 used, and further edge cases, for
each lane configuration. It compares exit status, `images_json`,
`image_count`, annotations and the build id configuration hash.
`images_json` matches `jq -c` byte for byte, extra keys included:
numbers keep their literal in `jq`'s canonical form (`1e-7` as
`1E-7`, `1.50` as written, large integers exact), as `jq` 1.7 and
later write them.

Where the inline step misbehaved, the action differs by design:

- Two or more invalid or colliding names share one annotation. Inline,
  every name after the first spilled onto a plain log line.
- `path_prefix: ./` names project-level images after the repository.
  Inline, it produced the invalid name `.`.
- A missing `path_prefix` fails with an annotation naming it, rather
  than a bare shell error.
- An empty `dockerfile` string defaults in the matrix and summary.
- Names with separator runs Docker rejects, such as `a..b` or
  `a___b`, fail here with their own message. Inline, they passed and
  failed later, at `docker tag`.
- `image_namespace` faces the same grammar at discovery, as the
  release lane applies it, rather than in the build loop.
- A repository name over Docker's 255-character limit, namespace
  included, fails here rather than at `docker tag`.
- `NaN` and `Infinity` in the `images` input are invalid input. Inline,
  `jq` turned them into `null`; passed through here, they would make
  `images_json` and the matrix unparsable.
- `path_prefix`, and each image's `context` and `dockerfile`, must
  resolve inside the workspace, symbolic links included. A context
  above `path_prefix`, such as `../shared`, remains valid.
- A per-image `platforms` takes a string or a list of strings, which
  joins with commas; any other type fails rather than falling back to
  the `platforms` input without a word. A platform containerd cannot
  parse fails here too, as `buildx` would later.
- Paths from the input or the tree reach the log and step summary
  escaped, so a line break or `|` in one cannot start a workflow
  command or rewrite the summary. Inline, both printed them verbatim.

## Notes

- Needs `python3` 3.10 or later on the runner, as GitHub-hosted
  runners provide, and fails with an annotation otherwise. The action
  needs nothing beyond the standard library, so it installs nothing.
- The walk sorts by Unicode code point on every runner, where the
  inline shell glob followed the runner's locale. The two agree on
  GitHub-hosted runners, which use `C.UTF-8`.
- The action reads files and never runs repository code. Python's
  isolated mode keeps the checked-out tree off the import path, so a
  repository carrying its own `scripts/` package cannot stand in for
  the action's code.

### Development

```bash
python3 -m unittest discover -s tests -t .
uv run --with pytest python -m pytest   # the same suite, via pytest
```

The equivalence tests need `bash`, `jq`, `sha256sum` and `od`, and skip
when those are missing; the number-literal cases also need `jq` 1.7
or later, as on `ubuntu-latest`.

[docker-workflows]: https://github.com/lfreleng-actions/docker-workflows
[docker-workflows#29]: https://github.com/lfreleng-actions/docker-workflows/issues/29
[docker-workflows#92]: https://github.com/lfreleng-actions/docker-workflows/pull/92
[pre-commit.ci results page]: https://results.pre-commit.ci/latest/github/lfreleng-actions/docker-build-matrix-action/main
[pre-commit.ci status badge]: https://results.pre-commit.ci/badge/github/lfreleng-actions/docker-build-matrix-action/main.svg
