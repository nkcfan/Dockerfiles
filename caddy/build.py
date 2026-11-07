#!/usr/bin/env python3
"""Build upstream base images locally for the checked-in Caddy Dockerfile."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parent
INSTALL_HASHES = {
    "alpine": "6a61f42f65db0bb24edd3340967990059670995f0942bd5db07bd9ad1579f6da",
    "builder": "3e09052b5bcb9e84357e91cfbd59a24dd92dc0f826e304c93a3105bfc8cd5a84",
}


def version(text, name, instruction="ENV"):
    matches = re.findall(
        r"^" + instruction + " " + name + r"=(v\d+\.\d+\.\d+)$", text, re.MULTILINE
    )
    if len(matches) != 1:
        raise ValueError(f"Expected one pinned stable {name} declaration")
    return matches[0]


def replace_install(text, variant, release, replacement):
    marker = 'RUN set -eux; \\\n\tapkArch='
    if text.count(marker) != 1:
        raise ValueError(f"{variant}: expected one release installation block")
    start = text.index(marker)
    end = text.find("\n\n", start)
    if end == -1:
        raise ValueError(f"{variant}: missing installation block boundary")
    block = text[start:end]
    normalized = re.sub(r"checksum='[0-9a-f]{128}'", "checksum='<SHA512>'", block)
    normalized = normalized.replace(release, "v<VERSION>")
    normalized = normalized.replace(release[1:], "<VERSION>")
    # Allow release/checksum updates, but require review of any other install changes.
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    if digest != INSTALL_HASHES[variant]:
        raise ValueError(
            f"{variant}: upstream installation changed (fingerprint {digest}); "
            "review the upstream diff before updating the transformation"
        )
    return text[:start] + replacement(block) + text[end:]


def transform(builder, runtime):
    caddy = version(runtime, "CADDY_VERSION")
    xcaddy = version(builder, "XCADDY_VERSION")
    if version(builder, "CADDY_VERSION") != caddy:
        raise ValueError("Builder and runtime Caddy versions differ")
    if not re.match(r"\AFROM golang:[^\s]+\n", builder):
        raise ValueError("Expected a single-stage upstream Go builder")
    if not re.match(r"\AFROM alpine:[^\s]+\n", runtime):
        raise ValueError("Expected a single-stage upstream Alpine runtime")
    if len(re.findall(r"^FROM ", builder + runtime, re.MULTILINE)) != 2:
        raise ValueError("Unexpected additional upstream build stages")

    source_builder = replace_install(
        builder,
        "builder",
        xcaddy,
        lambda block: (
            "RUN --mount=type=cache,target=/go/pkg/mod \\\n"
            "    --mount=type=cache,target=/root/.cache/go-build \\\n"
            "    set -eux; \\\n"
            "    GOTOOLCHAIN=local GOBIN=/usr/bin go install "
            'github.com/caddyserver/xcaddy/cmd/xcaddy@"${XCADDY_VERSION}"'
        ),
    )
    runtime_common = replace_install(runtime, "alpine", caddy, lambda block: "")
    result = (
        "# syntax=docker/dockerfile:1\n\n"
        + source_builder.replace("\n", " AS source-builder\n", 1)
        + "\n"
        + "FROM source-builder AS stock-caddy\n"
        + "RUN --mount=type=cache,target=/go/pkg/mod \\\n"
        + "    --mount=type=cache,target=/root/.cache/go-build \\\n"
        + '    CGO_ENABLED=0 GOTOOLCHAIN=local xcaddy build "${CADDY_VERSION}" '
        + "--output /usr/bin/caddy\n\n"
        + runtime_common.replace("\n", " AS runtime-common\n", 1)
        + "\n"
        + "FROM runtime-common AS source-runtime\n"
        + "COPY --from=stock-caddy /usr/bin/caddy /usr/bin/caddy\n"
        + "RUN setcap cap_net_bind_service=+p /usr/bin/caddy && caddy version\n\n"
    )
    if "/releases/download/" in result:
        raise ValueError("Unexpected release binary download remains in source stages")
    # BuildKit visits this release-binary stage only for the comparison target.
    result += builder.replace("\n", " AS reference-builder\n", 1)
    return result, {"caddy": caddy, "xcaddy": xcaddy}


def bake_config(output, revision):
    upstream = {
        "context": str(output.resolve()),
        "dockerfile": "Upstream.Dockerfile",
    }
    contexts = {
        "localhost/caddy-builder": "target:upstream-builder",
        "localhost/caddy-runtime": "target:upstream-runtime",
    }
    application = {"context": str(ROOT), "dockerfile": "Dockerfile"}
    return {
        "group": {"default": {"targets": ["caddy"]}},
        "target": {
            "upstream-builder": {
                **upstream, "target": "source-builder", "tags": ["localhost/caddy-builder"],
            },
            "upstream-runtime": {
                **upstream, "target": "source-runtime", "tags": ["localhost/caddy-runtime"],
            },
            "reference-builder": {**upstream, "target": "reference-builder"},
            "caddy": {
                **application, "contexts": contexts, "tags": ["caddy-source:test"],
                "labels": {"io.caddy-source.upstream-revision": revision},
            },
            "reference": {
                **application,
                "contexts": {
                    **contexts, "localhost/caddy-builder": "target:reference-builder",
                },
                "tags": ["caddy-upstream:test"],
            },
        },
    }


def generate(upstream, output, series=None):
    candidates = sorted(upstream.glob("*/alpine/Dockerfile"))
    if series is not None:
        if not re.fullmatch(r"\d+\.\d+", series):
            raise ValueError("--series must have the form MAJOR.MINOR")
        candidates = [path for path in candidates if path.parent.parent.name == series]
    if len(candidates) != 1:
        raise ValueError(
            "Expected one Alpine series; initialize the submodule or select --series. "
            f"Found: {[str(path.relative_to(upstream)) for path in candidates]}"
        )
    runtime_path = candidates[0]
    builder_path = runtime_path.parent.parent / "builder"
    builder = (builder_path / "Dockerfile").read_text()
    runtime = runtime_path.read_text()
    dockerfile, metadata = transform(builder, runtime)
    metadata["dynu"] = version((ROOT / "Dockerfile").read_text(), "DYNU_VERSION", "ARG")
    subprocess.run(
        ["git", "-C", str(upstream), "diff", "--exit-code", "HEAD", "--"],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Invalid upstream commit")
    metadata.update(
        upstream_revision=revision,
        series=runtime_path.parent.parent.name,
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "Upstream.Dockerfile").write_text(dockerfile)
    (output / "docker-bake.json").write_text(
        json.dumps(bake_config(output, revision), indent=2) + "\n"
    )
    (output / ".dockerignore").write_text("*\n!Upstream.Dockerfile\n!caddy-builder.sh\n")
    shutil.copy2(builder_path / "caddy-builder.sh", output / "caddy-builder.sh")
    (output / "versions.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=ROOT / "upstream")
    parser.add_argument("--output", type=Path, default=ROOT / ".build")
    parser.add_argument("--series")
    args = parser.parse_args()
    try:
        generate(args.upstream, args.output, args.series)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Source build generation failed: {error}\n")


if __name__ == "__main__":
    main()
