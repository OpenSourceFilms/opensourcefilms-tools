"""CPU-only contract tests for the historical Resolve CMX EDL marker converter.

Uses synthetic marker rows and disposable output; no footage, Resolve or API.
"""
from __future__ import annotations

import csv
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT / "bin" / "csv_to_resolve_markers_edl.py"


class ResolveMarkerEdlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="osf_edl_synthetic_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.csv_path = self.root / "markers.csv"
        self.edl_path = self.root / "markers.edl"

    def run_converter(self, rows, *flags):
        with self.csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["Timecode In", "Name", "Comment"])
            writer.writeheader()
            writer.writerows(rows)
        return subprocess.run(
            [sys.executable, "-B", str(CONVERTER),
             "--in_csv", str(self.csv_path), "--out_edl", str(self.edl_path), *flags],
            cwd=self.root, capture_output=True, text=True, timeout=10,
        )

    def marker(self, timecode, comment="fps=24"):
        return {"Timecode In": timecode, "Name": "SYNTHETIC|TEST", "Comment": comment}

    def test_valid_24_fps_frame_carries_only_when_marker_extends_past_second(self):
        out = self.run_converter(
            [self.marker("00:00:10:23")], "--marker_len_frames", "2"
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        text = self.edl_path.read_text(encoding="utf-8")
        self.assertIn("00:00:10:23 00:00:11:01", text)
        self.assertIn("FCM: NON-DROP FRAME", text)

    def test_valid_row_fps_override(self):
        out = self.run_converter([self.marker("00:00:10:24", comment="fps=25")])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("00:00:10:24 00:00:11:00", self.edl_path.read_text())

    def test_out_of_range_frame_is_rejected_without_creating_output(self):
        out = self.run_converter([self.marker("00:00:10:24")])
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("row 1", out.stderr)
        self.assertIn("outside", out.stderr)
        self.assertFalse(self.edl_path.exists())

    def test_bad_late_row_preserves_existing_export(self):
        self.edl_path.write_bytes(b"existing user EDL")
        out = self.run_converter([
            self.marker("00:00:10:01"),
            self.marker("00:00:11:29"),
        ])
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("row 2", out.stderr)
        self.assertEqual(self.edl_path.read_bytes(), b"existing user EDL")

    def test_zero_and_unsupported_fps_are_rejected(self):
        for comment, flags in [
            ("", ("--fps_default", "0")),
            ("", ("--fps_default", "-1")),
            ("", ("--fps_default", "61")),
            ("fps=0", ()),
            ("fps=61", ()),
            ("fps=23.976", ()),
        ]:
            with self.subTest(comment=comment, flags=flags):
                if self.edl_path.exists():
                    self.edl_path.unlink()
                out = self.run_converter([self.marker("00:00:00:00", comment)], *flags)
                self.assertNotEqual(out.returncode, 0)
                self.assertFalse(self.edl_path.exists())

    def test_zero_or_negative_marker_length_is_rejected(self):
        for length in ("0", "-1"):
            with self.subTest(length=length):
                out = self.run_converter(
                    [self.marker("00:00:10:00")], "--marker_len_frames", length
                )
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("must be positive", out.stderr)
                self.assertFalse(self.edl_path.exists())


if __name__ == "__main__":
    unittest.main()
