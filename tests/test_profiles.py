#!/usr/bin/env python3
"""Schema and registry smoke tests that do not require manufacturer firmware."""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "patch_ae.py"
SPEC = importlib.util.spec_from_file_location("patch_ae_release_test", MODULE_PATH)
assert SPEC and SPEC.loader
PATCH_AE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PATCH_AE
SPEC.loader.exec_module(PATCH_AE)


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        PATCH_AE.load_profile_registry([], trust_external=False)

    def test_release_version(self) -> None:
        self.assertEqual(PATCH_AE.__version__, "3.0.0")

    def test_official_registry(self) -> None:
        names = {layout.name for layout in PATCH_AE.FIRMWARE_LAYOUTS.values()}
        self.assertTrue(
            {
                "hc940-single-camera",
                "hc960-single-camera",
                "hc950-dual-camera-2024",
                "hc950-dual-camera-2026",
            }.issubset(names)
        )
        self.assertIn("hc940-ae58", PATCH_AE.PROFILES)
        self.assertIn("hc960-ae55", PATCH_AE.PROFILES)

    def test_json_profiles_have_context_fingerprints(self) -> None:
        for path in sorted((ROOT / "profiles").glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], 1)
            for candidate in payload["layout"]["candidates"]:
                self.assertEqual(len(candidate["expected_curve"]), 21)
                self.assertEqual(len(candidate["context_after_sha256"]), 64)
                self.assertEqual(len(candidate["over_exposure_sha256"]), 64)

    def test_expect_curve_parser(self) -> None:
        curve = ",".join(["110"] * 21)
        parsed = PATCH_AE.parse_expected_curve(f"0x1234={curve}")
        self.assertEqual(parsed.offset, 0x1234)
        self.assertEqual(parsed.curve, (110,) * 21)


if __name__ == "__main__":
    unittest.main()
