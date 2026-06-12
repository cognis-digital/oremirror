"""Smoke tests for oremirror. Standard library only, no network."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oremirror import (  # noqa: E402
    TOOL_NAME,
    TOOL_VERSION,
    parse_ref,
    parse_image_list,
    build_plan,
)
from oremirror.cli import main  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_DIR = os.path.join(REPO_ROOT, "demos", "01-basic")
DEMO_LIST = os.path.join(DEMO_DIR, "images.txt")
DEMO_FIX = os.path.join(DEMO_DIR, "fixtures.json")


def _fixtures():
    with open(DEMO_FIX, "r", encoding="utf-8") as fh:
        return json.load(fh)


class TestMetadata(unittest.TestCase):
    def test_metadata(self):
        self.assertEqual(TOOL_NAME, "oremirror")
        self.assertTrue(TOOL_VERSION)


class TestRefParsing(unittest.TestCase):
    def test_short_name_gets_library_namespace(self):
        ref = parse_ref("nginx:1.27")
        self.assertEqual(ref.repository, "library/nginx")
        self.assertEqual(ref.tag, "1.27")
        self.assertEqual(ref.registry, "registry-1.docker.io")

    def test_registry_with_port(self):
        ref = parse_ref("myreg.local:5000/team/app:v2")
        self.assertEqual(ref.registry, "myreg.local:5000")
        self.assertEqual(ref.repository, "team/app")
        self.assertEqual(ref.tag, "v2")

    def test_digest_pinned(self):
        d = "sha256:" + "a" * 64
        ref = parse_ref(f"ghcr.io/org/img@{d}")
        self.assertEqual(ref.digest, d)
        self.assertIsNone(ref.tag)
        self.assertEqual(ref.reference, d)

    def test_default_tag(self):
        self.assertEqual(parse_ref("quay.io/foo/bar").tag, "latest")

    def test_bad_digest_rejected(self):
        from oremirror import OreError
        with self.assertRaises(OreError):
            parse_ref("repo@sha256:nothex")


class TestImageList(unittest.TestCase):
    def test_text_list(self):
        refs = parse_image_list("alpine:v3.20\nnginx:1.27\n# comment\n")
        self.assertEqual(len(refs), 2)

    def test_yaml_block(self):
        raw = "images:\n  - alpine:v3.20\n  - nginx:1.27\n"
        refs = parse_image_list(raw)
        self.assertEqual({r.tag for r in refs}, {"v3.20", "1.27"})

    def test_dedup(self):
        refs = parse_image_list("nginx:1.27\nnginx:1.27\n")
        self.assertEqual(len(refs), 1)


class TestPlan(unittest.TestCase):
    def test_plan_over_fixtures(self):
        refs = parse_image_list("alpine:v3.20\nnginx:1.27\n")
        plan = build_plan(refs, fixtures=_fixtures())
        self.assertFalse(plan.failed)
        self.assertEqual(len(plan.images), 2)
        self.assertEqual(plan.total_blobs, 7)

    def test_missing_fixture_is_per_image_error(self):
        refs = parse_image_list("doesnotexist:9.9\n")
        plan = build_plan(refs, fixtures=_fixtures())
        self.assertTrue(plan.failed)
        self.assertIsNotNone(plan.images[0].error)


class TestPullVerify(unittest.TestCase):
    def test_pull_then_verify_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                main(["pull", DEMO_LIST, "--fixtures", DEMO_FIX, "-o", tmp]), 0)
            self.assertTrue(os.path.exists(os.path.join(tmp, "oci-layout")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "index.json")))
            self.assertEqual(main(["verify", tmp]), 0)

    def test_verify_fails_on_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            main(["pull", DEMO_LIST, "--fixtures", DEMO_FIX, "-o", tmp])
            blobs = os.path.join(tmp, "blobs", "sha256")
            victim = sorted(os.listdir(blobs))[0]
            with open(os.path.join(blobs, victim), "ab") as fh:
                fh.write(b"TAMPER")
            self.assertEqual(main(["verify", tmp]), 1)


class TestPushDryRun(unittest.TestCase):
    def test_push_dry_run_plans_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            main(["pull", DEMO_LIST, "--fixtures", DEMO_FIX, "-o", tmp])
            self.assertEqual(
                main(["push", tmp, "--dest", "reg.internal:5000", "--dry-run"]), 0)


class TestCliErrors(unittest.TestCase):
    def test_no_command_exits_2(self):
        self.assertEqual(main([]), 2)

    def test_missing_list_exits_2(self):
        self.assertEqual(main(["plan", "/no/such/list.txt"]), 2)

    def test_version_subprocess(self):
        proc = subprocess.run(
            [sys.executable, "-m", "oremirror", "--version"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(TOOL_VERSION, proc.stdout)


if __name__ == "__main__":
    unittest.main()
