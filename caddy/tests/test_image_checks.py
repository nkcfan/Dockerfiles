import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "image_checks", Path(__file__).resolve().parents[1] / "test_image.py"
)
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class ImageCheckTests(unittest.TestCase):
    def test_only_our_provenance_label_is_excluded(self):
        baseline = {"Labels": {"upstream": "kept"}, "Cmd": ["caddy", "run"]}
        source = {
            **baseline,
            "Labels": {**baseline["Labels"], checks.PROVENANCE_LABEL: "revision"},
        }
        checks.compare_configs(source, baseline, "revision")
        self.assertIn(checks.PROVENANCE_LABEL, source["Labels"])
        with self.assertRaisesRegex(ValueError, "metadata differs: Labels"):
            checks.compare_configs(
                {**source, "Labels": {**source["Labels"], "unexpected": "label"}},
                baseline, "revision",
            )

    def test_missing_provenance_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "incorrect upstream revision"):
            checks.compare_configs({}, {}, "revision")

    def test_runtime_changes_are_rejected(self):
        baseline = {"User": "", "Env": ["XDG_DATA_HOME=/data"]}
        source = {**baseline, "Labels": {checks.PROVENANCE_LABEL: "revision"}}
        for key, value in (("User", "nobody"), ("Env", ["XDG_DATA_HOME=/tmp"])):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "metadata differs: " + key):
                    checks.compare_configs({**source, key: value}, baseline, "revision")

    def test_wrong_response_does_not_pass_readiness(self):
        with patch.object(checks.urllib.request, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = b"wrong"
            with self.assertRaisesRegex(ValueError, "Unexpected response"):
                checks.wait_for_body("http://localhost", b"expected", "container")

    def test_smoke_test_verifies_tls_hostname_and_cleans_up(self):
        def command(*args):
            if args[0] == "create":
                return "container\n"
            if args[0] == "inspect":
                return json.dumps([{"NetworkSettings": {"Ports": {
                    "80/tcp": [{"HostPort": "8080"}],
                    "443/tcp": [{"HostPort": "8443"}],
                }}}])
            if args[:3] == ("exec", "container", "cat"):
                return "test certificate"
            return ""

        with patch.object(checks, "docker", side_effect=command) as docker:
            with patch.object(checks, "image_output", return_value="welcome"):
                with patch.object(checks.ssl, "create_default_context") as context:
                    with patch.object(checks, "wait_for_body") as wait:
                        checks.smoke_test("image")
        context.assert_called_once_with(cadata="test certificate")
        self.assertEqual(wait.call_args_list[1].args[0], "https://localhost:8443/")
        self.assertEqual(wait.call_count, 4)
        docker.assert_any_call("restart", "container")
        docker.assert_any_call("exec", "container", "test", "-s", "/config/caddy/autosave.json")
        docker.assert_called_with("rm", "--force", "--volumes", "container")

    def test_failed_start_still_removes_container_and_volumes(self):
        failure = subprocess.CalledProcessError(1, ["docker", "start"])
        with patch.object(checks, "image_output", return_value="welcome"):
            with patch.object(checks, "docker", side_effect=["container\n", failure, ""]) as docker:
                with self.assertRaises(subprocess.CalledProcessError):
                    checks.smoke_test("image")
        docker.assert_called_with("rm", "--force", "--volumes", "container")

    def test_validation_requires_exact_dynu_provider_version(self):
        metadata = {"caddy": "v2.11.9", "dynu": "v1.0.0", "upstream_revision": "revision"}

        def inspect(*args):
            labels = {checks.PROVENANCE_LABEL: "revision"} if args[-1] == "source" else {}
            return json.dumps([{
                "Architecture": "amd64", "Os": "linux", "Config": {"Labels": labels},
            }])

        for modules, valid in (
            ("", False),
            ("dns.providers.dynu v9.9.9\n", False),
            ("dns.providers.dynu v1.0.0\n", True),
        ):
            with self.subTest(modules=modules):
                def output(image, *args):
                    if args[:2] == ("caddy", "list-modules"):
                        return modules
                    if args[:2] == ("caddy", "version"):
                        return metadata["caddy"] + " test-build\n"
                    return "matching inventory"

                with patch.object(checks, "docker", side_effect=inspect):
                    with patch.object(checks, "image_output", side_effect=output):
                        with patch.object(checks, "smoke_test") as smoke:
                            if valid:
                                checks.validate("source", "baseline", metadata)
                                self.assertEqual(smoke.call_count, 2)
                            else:
                                with self.assertRaisesRegex(ValueError, "pinned Dynu"):
                                    checks.validate("source", "baseline", metadata)
                                smoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
