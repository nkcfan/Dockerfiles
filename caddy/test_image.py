#!/usr/bin/env python3
"""Compare a source-built image with locally built upstream packaging."""

import argparse
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
PROVENANCE_LABEL = "io.caddy-source.upstream-revision"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True)


def image_output(image, *args):
    return docker("run", "--rm", "--network", "none", image, *args)


def compare_configs(source, baseline, revision):
    if source.get("Labels", {}).get(PROVENANCE_LABEL) != revision:
        raise ValueError("Source image has incorrect upstream revision")
    source = dict(source)
    source["Labels"] = {
        key: value for key, value in source.get("Labels", {}).items()
        if key != PROVENANCE_LABEL
    }
    keys = (
        "Env", "Cmd", "Entrypoint", "WorkingDir", "User", "ExposedPorts",
        "Volumes", "Labels", "StopSignal", "Healthcheck",
    )
    for key in keys:
        if source.get(key) != baseline.get(key):
            raise ValueError(f"Runtime metadata differs: {key}")


def wait_for_body(url, expected, container, context=None):
    deadline = time.monotonic() + 60
    error = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3, context=context) as response:
                body = response.read()
                if body != expected:
                    raise ValueError(f"Unexpected response from {url}: {body[:200]!r}")
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as failure:
            error = failure
            time.sleep(1)
    logs = docker("logs", container)
    raise RuntimeError(f"Server did not become ready at {url}: {error}\n{logs}")


def smoke_test(image):
    welcome = image_output(image, "cat", "/usr/share/caddy/index.html").encode()
    image_output(image, "caddy", "validate", "--config", "/etc/caddy/Caddyfile",
                 "--adapter", "caddyfile")
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "Caddyfile"
        config.write_text(
            "http://:80 {\n"
            "    root * /usr/share/caddy\n"
            "    file_server\n"
            "}\n"
            "https://localhost {\n"
            "    tls internal\n"
            '    respond "https-ok"\n'
            "}\n"
        )
        container = docker(
            "create",
            "--publish", "127.0.0.1::80",
            "--publish", "127.0.0.1::443",
            "--volume", "/data", "--volume", "/config",
            "--mount", f"type=bind,source={config},target=/tmp/smoke.Caddyfile,readonly",
            image, "caddy", "run", "--config", "/tmp/smoke.Caddyfile",
            "--adapter", "caddyfile",
        ).strip()
        try:
            docker("start", container)
            ports = json.loads(docker("inspect", container))[0]["NetworkSettings"]["Ports"]
            http_port = ports["80/tcp"][0]["HostPort"]
            https_port = ports["443/tcp"][0]["HostPort"]
            http = f"http://127.0.0.1:{http_port}/"
            https = f"https://localhost:{https_port}/"
            wait_for_body(http, welcome, container)
            certificate = docker(
                "exec", container, "cat", "/data/caddy/pki/authorities/local/root.crt"
            )
            context = ssl.create_default_context(cadata=certificate)
            wait_for_body(https, b"https-ok", container, context)
            docker("restart", container)
            wait_for_body(http, welcome, container)
            wait_for_body(https, b"https-ok", container, context)
            if docker(
                "exec", container, "cat", "/data/caddy/pki/authorities/local/root.crt"
            ) != certificate:
                raise ValueError("Local CA did not persist across restart")
            docker("exec", container, "test", "-s", "/config/caddy/autosave.json")
        finally:
            docker("rm", "--force", "--volumes", container)


def validate(image, baseline, metadata):
    source_info = json.loads(docker("image", "inspect", image))[0]
    baseline_info = json.loads(docker("image", "inspect", baseline))[0]
    for key in ("Architecture", "Os"):
        if source_info[key] != baseline_info[key]:
            raise ValueError(f"Image platform differs: {key}")
    compare_configs(
        source_info["Config"], baseline_info["Config"], metadata["upstream_revision"]
    )
    inventory = (
        "apk info -vv | sort; "
        "sha256sum /etc/caddy/Caddyfile /usr/share/caddy/index.html "
        "/etc/ssl/certs/ca-certificates.crt; "
        "stat -c '%a:%u:%g' /config/caddy /data/caddy /usr/bin/caddy; "
        "getcap /usr/bin/caddy"
    )
    if image_output(image, "sh", "-ec", inventory) != image_output(
        baseline, "sh", "-ec", inventory
    ):
        raise ValueError("Runtime packages, assets, permissions, or capabilities differ")
    modules = image_output(image, "caddy", "list-modules", "--versions")
    if modules != image_output(
        baseline, "caddy", "list-modules", "--versions"
    ):
        raise ValueError("Caddy module inventory differs")
    dynu = [
        line.split() for line in modules.splitlines()
        if line.split() and line.split()[0] == "dns.providers.dynu"
    ]
    if len(dynu) != 1 or metadata["dynu"] not in dynu[0][1:]:
        raise ValueError("Expected pinned Dynu DNS provider is missing")
    for target in (image, baseline):
        actual = image_output(target, "caddy", "version").split()[0]
        if actual != metadata["caddy"]:
            raise ValueError(f"{target}: expected {metadata['caddy']}, got {actual}")
        smoke_test(target)
    image_output(
        image, "sh", "-ec",
        "for tool in go xcaddy git; do "
        'if command -v "$tool"; then echo "Unexpected runtime tool: $tool" >&2; exit 1; fi; '
        "done",
    )
    print(f"Runtime equivalence and HTTP/HTTPS persistence checks passed: {image}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="caddy-source:test")
    parser.add_argument("--baseline", default="caddy-upstream:test")
    parser.add_argument("--metadata", type=Path, default=ROOT / ".build" / "versions.json")
    args = parser.parse_args()
    validate(args.image, args.baseline, json.loads(args.metadata.read_text()))


if __name__ == "__main__":
    main()
