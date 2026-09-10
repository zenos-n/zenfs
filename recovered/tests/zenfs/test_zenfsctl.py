import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CLI = Path(os.environ.get("ZENFSCTL_SCRIPT", Path(__file__).parents[2] / "packages" / "zenfsctl" / "zenfsctl.py"))


class ZenFSCTLTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            check=False,
            text=True,
            capture_output=True,
        )

    def test_status_reports_correct_and_clobbered_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            good = root / "good"
            bad = root / "bad"
            good.symlink_to(target)
            bad.mkdir()
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": "zenfs-v1",
                        "aliases": {str(good): str(target), str(bad): str(target)},
                        "roaming": {
                            "work": {
                                "enable": True,
                                "mountPoint": str(root / "unmounted"),
                                "markerFile": ".zenfs-roaming.json",
                                "markerId": "work",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_cli("status", "--manifest", str(manifest), "--json")

            self.assertEqual(result.returncode, 1)
            output = json.loads(result.stdout)
            self.assertFalse(output["healthy"])
            self.assertEqual(
                {item["status"] for item in output["aliases"]}, {"ok", "clobber"}
            )
            self.assertEqual(output["roaming"][0]["status"], "unmounted")

    def test_verify_marker_checks_schema_and_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "marker.json"
            marker.write_text(
                json.dumps({"schema": "zenfs-roaming-v1", "id": "work"}),
                encoding="utf-8",
            )

            valid = self.run_cli("verify-marker", str(marker), "--id", "work")
            invalid = self.run_cli("verify-marker", str(marker), "--id", "other")

            self.assertEqual(valid.returncode, 0)
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("expected 'other'", invalid.stderr)

    def test_migrate_hierarchy_is_atomic_and_no_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "data").write_text("kept", encoding="utf-8")

            moved = self.run_cli("migrate-hierarchy", str(source), str(destination))

            self.assertEqual(moved.returncode, 0, moved.stderr)
            self.assertFalse(os.path.lexists(source))
            self.assertEqual((destination / "data").read_text(encoding="utf-8"), "kept")

            replacement = root / "replacement"
            replacement.mkdir()
            blocked = self.run_cli(
                "migrate-hierarchy", str(replacement), str(destination)
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertTrue(replacement.is_dir())
            self.assertEqual((destination / "data").read_text(encoding="utf-8"), "kept")


if __name__ == "__main__":
    unittest.main()
