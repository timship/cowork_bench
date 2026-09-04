#!/usr/bin/env python3
"""Unit tests for Harbor-canonical generator helpers."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from generate_harbor_canonical import (  # noqa: E402
    EMPTY_GROUNDTRUTH,
    NO_GROUNDTRUTH,
    _usable_groundtruth,
)


class UsableGroundtruthTests(unittest.TestCase):
    def test_missing_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(_usable_groundtruth(Path(td)))

    def test_only_gitkeep(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = root / "groundtruth_workspace"
            g.mkdir()
            (g / ".gitkeep").write_text("", encoding="utf-8")
            self.assertFalse(_usable_groundtruth(root))

    def test_real_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = root / "groundtruth_workspace"
            g.mkdir()
            (g / "out.xlsx").write_bytes(b"PK")
            self.assertTrue(_usable_groundtruth(root))

    def test_policy_sets_disjoint_from_usable_expectation(self) -> None:
        # Empty-GT sources must not be confused with the fixed GitHub-25 list.
        self.assertTrue(EMPTY_GROUNDTRUTH.isdisjoint(NO_GROUNDTRUTH))
        self.assertIn("rzd-hr1c-training-trip-kazan-excel-email-gcal", EMPTY_GROUNDTRUTH)


if __name__ == "__main__":
    unittest.main()
