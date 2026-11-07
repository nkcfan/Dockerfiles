import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("caddy_build", ROOT / "build.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class SourceBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        candidates = list((ROOT / "upstream").glob("*/alpine/Dockerfile"))
        if len(candidates) != 1:
            raise RuntimeError("Initialize upstream; review tests if it adds multiple series")
        cls.runtime = candidates[0].read_text()
        cls.builder = (
            candidates[0].parent.parent / "builder" / "Dockerfile"
        ).read_text()

    def test_source_compilation_and_runtime_preservation(self):
        result, metadata = build.transform(self.builder, self.runtime)
        self.assertIn('go install github.com/caddyserver/xcaddy/cmd/xcaddy@"', result)
        self.assertIn('xcaddy build "${CADDY_VERSION}"', result)
        self.assertIn("CGO_ENABLED=0 GOTOOLCHAIN=local", result)
        source, reference = result.split("\nFROM golang:", 2)[1:]
        self.assertNotIn("/releases/download/", source)
        self.assertIn("/releases/download/", reference)
        self.assertNotIn("FROM caddy", result)
        runtime = source.split("\nFROM alpine:", 1)[1]
        original = self.runtime.split("\n\n")
        generated = ("FROM alpine:" + runtime).replace(
            " AS runtime-common", "", 1
        ).split("\n\n")
        for block in original:
            if '\tapkArch=' not in block:
                self.assertIn(block.rstrip("\n"), [item.rstrip("\n") for item in generated])
        self.assertIn(
            "FROM runtime-common AS source-runtime\n"
            "COPY --from=stock-caddy /usr/bin/caddy /usr/bin/caddy\n"
            "RUN setcap cap_net_bind_service=+p /usr/bin/caddy",
            result,
        )
        self.assertNotIn("go install", runtime)
        self.assertEqual(metadata["caddy"], build.version(self.runtime, "CADDY_VERSION"))

    def test_release_and_checksum_updates_need_no_patch_changes(self):
        caddy = build.version(self.runtime, "CADDY_VERSION")
        xcaddy = build.version(self.builder, "XCADDY_VERSION")
        builder = self.builder.replace(caddy, "v99.1.2").replace(caddy[1:], "99.1.2")
        builder = builder.replace(xcaddy, "v9.1.2").replace(xcaddy[1:], "9.1.2")
        runtime = self.runtime.replace(caddy, "v99.1.2").replace(caddy[1:], "99.1.2")
        builder = re.sub(r"checksum='[0-9a-f]{128}'", "checksum='" + "a" * 128 + "'", builder)
        runtime = re.sub(r"checksum='[0-9a-f]{128}'", "checksum='" + "b" * 128 + "'", runtime)
        _, metadata = build.transform(builder, runtime)
        self.assertEqual(
            metadata,
            {"caddy": "v99.1.2", "xcaddy": "v9.1.2"},
        )

    def test_installation_changes_fail_closed(self):
        for source in ("builder", "runtime"):
            with self.subTest(source=source):
                builder = self.builder
                runtime = self.runtime
                if source == "builder":
                    builder = builder.replace("chmod +x", "chmod 777")
                else:
                    runtime = runtime.replace("setcap cap_net_bind_service=+p", "setcap cap_net_bind_service=+ep")
                with self.assertRaisesRegex(ValueError, "upstream installation changed"):
                    build.transform(builder, runtime)

    def test_version_mismatch_is_rejected(self):
        builder = self.builder.replace(
            "ENV CADDY_VERSION=" + build.version(self.runtime, "CADDY_VERSION"),
            "ENV CADDY_VERSION=v99.0.0",
        )
        with self.assertRaisesRegex(ValueError, "versions differ"):
            build.transform(builder, self.runtime)

    def test_unpinned_versions_are_rejected(self):
        runtime = self.runtime.replace(
            "ENV CADDY_VERSION=" + build.version(self.runtime, "CADDY_VERSION"),
            "ENV CADDY_VERSION=latest",
        )
        with self.assertRaisesRegex(ValueError, "pinned stable"):
            build.transform(self.builder, runtime)

    def test_dynu_pin_is_owned_by_checked_in_dockerfile(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        pin = build.version(dockerfile, "DYNU_VERSION", "ARG")
        self.assertIn('--with "github.com/caddy-dns/dynu@${DYNU_VERSION}"', dockerfile)
        self.assertIn("FROM caddy:builder AS builder", dockerfile)
        self.assertIn("FROM caddy:latest", dockerfile)
        self.assertIn("RUN xcaddy build \\\n", dockerfile)
        self.assertNotIn("ARG CADDY_VERSION", dockerfile)
        self.assertEqual(
            build.version(dockerfile.replace(pin, "v9.8.7"), "DYNU_VERSION", "ARG"),
            "v9.8.7",
        )
        with self.assertRaisesRegex(ValueError, "pinned stable"):
            build.version(dockerfile.replace(pin, "latest"), "DYNU_VERSION", "ARG")

    def test_extra_stages_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "additional upstream build stages"):
            build.transform(self.builder + "\nFROM caddy:latest\n", self.runtime)

    def test_generation_records_revision_and_limits_context(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build.generate(ROOT / "upstream", output)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {".dockerignore", "Upstream.Dockerfile", "docker-bake.json",
                 "caddy-builder.sh", "versions.json"},
            )
            metadata = json.loads((output / "versions.json").read_text())
            self.assertRegex(metadata["upstream_revision"], r"^[0-9a-f]{40}$")
            bake = json.loads((output / "docker-bake.json").read_text())
            source = bake["target"]["caddy"]
            reference = bake["target"]["reference"]
            self.assertEqual(
                source["labels"]["io.caddy-source.upstream-revision"],
                metadata["upstream_revision"],
            )
            self.assertEqual(source["context"], str(ROOT))
            self.assertEqual(source["dockerfile"], "Dockerfile")
            builder_image = "caddy:builder"
            runtime_image = "caddy:latest"
            self.assertNotIn("args", source)
            self.assertEqual(
                source["contexts"][builder_image], "target:upstream-builder"
            )
            self.assertEqual(
                reference["contexts"][builder_image], "target:reference-builder"
            )
            self.assertEqual(
                source["contexts"][runtime_image],
                reference["contexts"][runtime_image],
            )
            self.assertEqual(bake["target"]["upstream-runtime"]["target"], "source-runtime")
            self.assertEqual((output / ".dockerignore").read_text(),
                             "*\n!Upstream.Dockerfile\n!caddy-builder.sh\n")

    def test_unversioned_aliases_keep_upstream_caddy_version(self):
        caddy = build.version(self.runtime, "CADDY_VERSION")
        builder = self.builder.replace(caddy, "v99.1.2").replace(caddy[1:], "99.1.2")
        runtime = self.runtime.replace(caddy, "v99.1.2").replace(caddy[1:], "99.1.2")
        result, metadata = build.transform(builder, runtime)
        self.assertEqual(metadata["caddy"], "v99.1.2")
        source, reference = result.split("\nFROM golang:", 2)[1:]
        for stage in (source, reference):
            self.assertIn("ENV CADDY_VERSION=v99.1.2\n", stage)
            self.assertNotIn("ENV CADDY_VERSION=99.1.2", stage)
        config = build.bake_config(ROOT / ".build", "revision")
        target = config["target"]["caddy"]
        self.assertNotIn("args", target)
        self.assertEqual(target["contexts"], {
            "caddy:builder": "target:upstream-builder",
            "caddy:latest": "target:upstream-runtime",
        })

    def test_ambiguous_series_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            upstream = Path(directory)
            for series in ("2.11", "2.12"):
                path = upstream / series / "alpine"
                path.mkdir(parents=True)
                (path / "Dockerfile").touch()
            with self.assertRaisesRegex(ValueError, "Expected one Alpine series"):
                build.generate(upstream, upstream / "output")


if __name__ == "__main__":
    unittest.main()
