#!/usr/bin/env python3
"""Generate a source-only build while preserving upstream runtime packaging."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parent
DYNU_VERSION = "v1.0.0"
INSTALL_HASHES = {
    "alpine": "6a61f42f65db0bb24edd3340967990059670995f0942bd5db07bd9ad1579f6da",
    "builder": "3e09052b5bcb9e84357e91cfbd59a24dd92dc0f826e304c93a3105bfc8cd5a84",
}


def version(text, name):
    matches = re.findall(r"^ENV " + name + r"=(v\d+\.\d+\.\d+)$", text, re.MULTILINE)
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


def caddy_command():
    if not re.fullmatch(r"v\d+\.\d+\.\d+", DYNU_VERSION):
        raise ValueError("Expected one pinned stable DYNU_VERSION")
    return (
        'CGO_ENABLED=0 GOTOOLCHAIN=local xcaddy build "${CADDY_VERSION}" '
        f'--with "github.com/caddy-dns/dynu@{DYNU_VERSION}" --output /usr/bin/caddy'
    )


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

    builder = replace_install(
        builder,
        "builder",
        xcaddy,
        lambda block: (
            "RUN --mount=type=cache,target=/go/pkg/mod \\\n"
            "    --mount=type=cache,target=/root/.cache/go-build \\\n"
            "    set -eux; \\\n"
            "    GOTOOLCHAIN=local GOBIN=/usr/bin go install "
            'github.com/caddyserver/xcaddy/cmd/xcaddy@"${XCADDY_VERSION}"; \\\n'
            f"    {caddy_command()}; \\\n"
            "    caddy version"
        ),
    )
    builder = builder.replace("\n", " AS source-builder\n", 1)

    def runtime_install(block):
        tail = block[block.index("\tsetcap cap_net_bind_service="):]
        return (
            "COPY --from=source-builder /usr/bin/caddy /usr/bin/caddy\n\n"
            "RUN set -eux; \\\n" + tail
        )

    runtime = replace_install(runtime, "alpine", caddy, runtime_install)
    result = "# syntax=docker/dockerfile:1\n\n" + builder + "\n" + runtime
    if "/releases/download/" in result or "/tmp/xcaddy.tar.gz" in result:
        raise ValueError("Unexpected release binary download remains")
    return result, {"caddy": caddy, "xcaddy": xcaddy, "dynu": DYNU_VERSION}


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
    baseline = (
        builder.replace("\n", " AS reference-builder\n", 1)
        + f"\nRUN {caddy_command()}\n\n"
        + runtime
        + "\nCOPY --from=reference-builder /usr/bin/caddy /usr/bin/caddy\n"
        + "RUN setcap cap_net_bind_service=+p /usr/bin/caddy\n"
    )
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
    dockerfile += f'\nLABEL io.caddy-source.upstream-revision="{revision}"\n'
    output.mkdir(parents=True, exist_ok=True)
    (output / "Dockerfile").write_text(dockerfile)
    (output / "Baseline.Dockerfile").write_text(baseline)
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
