"""Offline, read-only checks that the public legacy entrypoints fail closed."""
from __future__ import annotations
import ast
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = [
    "scripts/setup_infrastructure.sh",
    "scripts/setup_phase1.sh",
    "scripts/start_services.sh",
    "scripts/start_api.sh",
]


class PublicArchiveSafety(unittest.TestCase):
    def test_archived_entrypoints_fail_before_side_effects(self):
        for item in ENTRYPOINTS:
            with self.subTest(script=item):
                script = ROOT / item
                body = script.read_text(encoding="utf-8")
                lines = body.splitlines()
                self.assertEqual(lines[0], "#!/usr/bin/env bash")
                # Exactly two comments, one printf and an unconditional exit
                # before the archived original script can execute any action.
                self.assertTrue(lines[1].startswith("# PUBLIC ARCHIVE SAFETY GATE:"))
                self.assertTrue(lines[2].startswith("# Do not remove this guard"))
                self.assertTrue(lines[3].startswith("printf '%s\\n' 'REFUSED:"))
                self.assertEqual(lines[4], "exit 78")
                subprocess.run(["bash", "-n", str(script)], check=True,
                               capture_output=True, timeout=10)
                # The static preamble was verified above; execute it in the
                # offline test context to assert an actual exit 78.
                stopped = subprocess.run(["bash", str(script)], cwd=ROOT,
                                         capture_output=True, text=True, timeout=5)
                self.assertEqual(stopped.returncode, 78, item)
                self.assertIn("REFUSED", stopped.stderr)

    def test_settings_require_explicit_credentials(self):
        root = ast.parse((ROOT / "config/settings.py").read_text(encoding="utf-8"))
        settings = next(n for n in root.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
        found = {}
        for node in settings.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found[node.target.id] = node.value
        for key in ("postgres_url", "minio_access_key", "minio_secret_key"):
            with self.subTest(setting=key):
                self.assertIn(key, found)
                self.assertIsNone(found[key], "credential-bearing settings must not have defaults")

    def test_archive_is_prominently_labelled(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("Historical, superseded project snapshot", readme)
        self.assertIn("Do not deploy", readme)
        self.assertIn("exit code 78", security)
        self.assertIn("not a supported service", " ".join(security.split()))

    def test_silence_fixture_is_reproducible(self):
        script = ROOT / "scripts/generate_silence_fixture.py"
        self.assertTrue(script.is_file())
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Verified reproducible PCM silence", proc.stdout)

    def test_historical_third_party_notice_present(self):
        notice = (ROOT / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        wrapper = (ROOT / "bin/whispercpp").read_text(encoding="utf-8")
        self.assertIn("THIRD_PARTY_LICENSES.md", readme)
        self.assertIn("bin/silence.wav", notice)
        self.assertIn("intfloat/e5-small-v2", notice)
        self.assertIn("insightface/buffalo_l", notice)
        self.assertIn("transnetv2_pytorch", notice)
        self.assertIn("GGML", notice)
        self.assertIn("whisper-cli", wrapper)
        self.assertTrue(wrapper.startswith("#!/usr/bin/env bash"))
        self.assertNotIn("third-party notice file will be added", readme.lower())

    def test_security_document_covers_all_disabled_entrypoints(self):
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        for item in ENTRYPOINTS:
            self.assertIn(item, security)


if __name__ == "__main__":
    unittest.main()
