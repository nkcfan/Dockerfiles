# Caddy with Dynu, built from source

This integration never pulls a Caddy Docker Hub image. It compiles both xcaddy
and Caddy from source, using versions and Go/Alpine bases selected by the pinned
[upstream packaging](https://github.com/caddyserver/caddy-docker) submodule.
Only Caddy (including the Dynu DNS provider) and upstream's runtime payload enter
the final stage. The Dynu version is pinned in `build.py`.

The compatibility baseline recreates the previous Dynu-enabled build using
upstream Dockerfiles at the recorded submodule commit. It uses upstream's
release xcaddy binary to compile Caddy with the same pinned Dynu module.
Unlike the previous binary-overlay Dockerfile, both builds explicitly restore
the Caddy binary's low-port capability after copying it into the final stage.
Go/Alpine images and OS packages are still prebuilt; this is not a bootstrap of
the entire toolchain or an offline build.

## Local build and verification

Requirements: Git, Python 3.9+, Docker with BuildKit, and network access to base
images, Alpine packages, GitHub assets, and Go module sources.
Run from the repository root:

```sh
git submodule update --init --recursive
python3 -m unittest discover -s caddy/tests -v
python3 caddy/build.py
docker build --tag caddy-source:test caddy/.build
docker build --file caddy/.build/Baseline.Dockerfile \
  --tag caddy-upstream:test caddy/.build
python3 caddy/test_image.py
```

The verification baseline downloads official release binaries from GitHub,
not Docker Hub. Those binaries are used only in the comparison image, never in
the generated source-build image.

The generated build context contains only the source and baseline Dockerfiles,
upstream builder helper, and version metadata. It never includes credentials,
deployment configuration, or runtime data from the surrounding directory.
Generated files stay in the ignored `.build/` directory. The old hand-maintained
Dockerfile is replaced by this generation step; build from `caddy/.build`, not `caddy`.
The source stage uses `CGO_ENABLED=0` and the selected Go toolchain; it does not
silently download a newer Go toolchain.

The generator preserves upstream runtime setup and replaces only the two release
installation blocks. Their fingerprints normalize version strings and SHA-512
checksums, allowing routine releases without patch churn. Other changes to these
blocks fail generation and require review. Permissions and file capabilities are
applied in the final stage after the binary is copied.

## CI and publication

[The workflow](../.github/workflows/caddy.yml) builds and tests natively on Linux
amd64 and arm64. It compares image configuration, packages, bundled assets,
permissions, capabilities, versions, and module inventory with upstream packaging.
It also compares the exported root filesystem: file contents, types, ownership,
permissions, and symlink targets must match. Only Caddy's binary contents (rebuilt
from source) and Docker-generated hostname/hosts/resolver files are excluded;
timestamps are not compared. APK's logged command timestamp is normalized, but
its commands, package versions, and other log contents must still match.
Caddy binary permissions and capabilities are still checked.
It also tests HTTP serving, internal-CA HTTPS, restart persistence, and absence of
build tools in the runtime image.

For migration verification against an actual previously published image, dispatch
the Caddy workflow on the PR branch with `reference_image` set to an immutable
`ghcr.io/nkcfan/caddy@sha256:...` digest. Both architectures must pass the same
metadata, payload, module-version, and runtime checks against that image. This is
an explicit check for matching versions, not a comparison of different releases.

The reusable Caddy workflow remains part of the parent `image-checks` gate.
Default-branch builds publish the **tested images**, without rebuilding, to:

```text
ghcr.io/<owner>/caddy:<Caddy version without v>-<commit>
ghcr.io/<owner>/caddy:<Caddy version without v>-<commit>-run-<run ID>-<attempt>
ghcr.io/<owner>/caddy:latest
```

The first successful GitHub Actions run requires Actions and GHCR package writes
to be allowed. The parent workflow listens for pushes to `main`, PRs, and manual
dispatch; Caddy retains individual manual dispatch. Pull requests cannot publish images.
Per-architecture tags are also retained. Version and `latest` tags may move;
deploy and roll back using an image digest or the unique run tag.

## Automated updates

Enable the Renovate GitHub App for this repository. [Renovate configuration](../renovate.json)
enables its opt-in submodule manager, existing Dynu/LazyDocker version managers,
and GitHub Actions updates. It watches the
upstream `master` branch and proposes changes to the recorded submodule commit.
It does not directly watch Caddy releases, so it follows upstream packaging's timing.

Protect the default branch and require the existing `image-checks` gate.
Automerge is disabled initially for the Caddy packaging submodule only; existing
Dynu, LazyDocker, and action update policies are retained. To make packaging
updates unattended, enable it for that submodule after required checks and merge
permissions are configured. Renovate does not fix integration incompatibilities.

Normal builds must **not** use `git submodule update --remote`.
To deliberately update manually:

```sh
git -C caddy/upstream fetch origin master
git -C caddy/upstream checkout --detach origin/master
python3 -m unittest discover -s caddy/tests -v
python3 caddy/build.py
# Run Docker verification above, then record the new pin:
git add caddy/upstream
```

If upstream changes an installation block, inspect its diff before adapting
`build.py` and its fingerprint. Do not merely accept the new hash: account for
new behavior and extend tests. Multiple upstream Alpine series require an explicit
selection with `--series MAJOR.MINOR` and matching updates to CI/tests. Unexpected
paths and unsupported version formats stop the build instead of guessing.

The pin fixes source/packaging versions, not every external input: base tags and
Alpine package repositories remain mutable. Bit-identical historical images need
additional base-digest/package-snapshot pinning. This integration verifies
functional/runtime compatibility, not binary or image digest identity.
