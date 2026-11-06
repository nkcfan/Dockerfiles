import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch


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
        self.assertIn(f'--with "github.com/caddy-dns/dynu@{build.DYNU_VERSION}"', result)
        self.assertNotIn("/releases/download/", result)
        self.assertNotIn("FROM caddy", result)
        runtime = result.split("\nFROM alpine:", 1)[1]
        original = self.runtime.split("\n\n")
        generated = ("FROM alpine:" + runtime).split("\n\n")
        for block in original:
            if '\tapkArch=' not in block:
                self.assertIn(block, generated)
        self.assertIn(
            "COPY --from=source-builder /usr/bin/caddy /usr/bin/caddy\n\n"
            "RUN set -eux; \\\n\tsetcap cap_net_bind_service=+p /usr/bin/caddy;",
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
            {"caddy": "v99.1.2", "xcaddy": "v9.1.2", "dynu": build.DYNU_VERSION},
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

    def test_dynu_updates_are_pinned_in_command_and_metadata(self):
        with patch.object(build, "DYNU_VERSION", "v9.8.7"):
            result, metadata = build.transform(self.builder, self.runtime)
        self.assertIn('--with "github.com/caddy-dns/dynu@v9.8.7"', result)
        self.assertEqual(metadata["dynu"], "v9.8.7")
        with patch.object(build, "DYNU_VERSION", "latest"):
            with self.assertRaisesRegex(ValueError, "pinned stable DYNU_VERSION"):
                build.transform(self.builder, self.runtime)

    def test_extra_stages_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "additional upstream build stages"):
            build.transform(self.builder + "\nFROM caddy:latest\n", self.runtime)

    def test_generation_records_revision_and_limits_context(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build.generate(ROOT / "upstream", output)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"Dockerfile", "Baseline.Dockerfile", "caddy-builder.sh", "versions.json"},
            )
            metadata = json.loads((output / "versions.json").read_text())
            self.assertRegex(metadata["upstream_revision"], r"^[0-9a-f]{40}$")
            self.assertIn(metadata["upstream_revision"], (output / "Dockerfile").read_text())
            baseline = (output / "Baseline.Dockerfile").read_text()
            self.assertIn("/releases/download/", baseline)
            self.assertNotIn("FROM caddy:", baseline)
            self.assertIn(f'--with "github.com/caddy-dns/dynu@{build.DYNU_VERSION}"', baseline)
            self.assertIn("RUN setcap cap_net_bind_service=+p /usr/bin/caddy", baseline)

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
