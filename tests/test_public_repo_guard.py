"""Keep private Windows enrollment/configuration out of a public snapshot."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_guard", REPO / "scripts/check_public_repo.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class PublicRepoGuardTests(unittest.TestCase):
    def test_forced_tracked_enrollment_and_config_backups_are_rejected(self):
        # Fake values only. Tracked files must be rejected even if ignore rules exist.
        for name in ("templates/gateway-token.dpapi", "templates/config.toml",
                     "templates/config.toml.before-change"):
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="sios-public-guard-") as temp:
                root = Path(temp)
                subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
                target = root / name
                target.parent.mkdir(parents=True)
                target.write_text("synthetic fixture only", encoding="utf-8")
                subprocess.run(["git", "-C", str(root), "add", "--force", name], check=True, capture_output=True)
                output = io.StringIO()
                with patch.object(guard, "ROOT", root), contextlib.redirect_stderr(output):
                    self.assertEqual(guard.main(), 1)
                self.assertIn("private or generated path", output.getvalue())

    def test_ignore_rules_cover_private_enrollment_and_config_backups(self):
        with tempfile.TemporaryDirectory(prefix="sios-public-ignore-") as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            (root / ".gitignore").write_bytes((REPO / ".gitignore").read_bytes())
            for name in ("gateway-token.dpapi", "config.toml", "config.toml.before-change",
                         "private/config.toml", "templates/gateway-token.dpapi"):
                result = subprocess.run(["git", "-C", str(root), "check-ignore", "--no-index", name], capture_output=True)
                self.assertEqual(result.returncode, 0, name)
            result = subprocess.run(["git", "-C", str(root), "check-ignore", "--no-index",
                                     "templates/codex-hindsight-relay.example.toml"], capture_output=True)
            self.assertEqual(result.returncode, 1)

    def test_generic_relay_example_remains_publishable(self):
        with tempfile.TemporaryDirectory(prefix="sios-public-template-") as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            target = root / "templates/codex-hindsight-relay.example.toml"
            target.parent.mkdir(parents=True)
            target.write_bytes((REPO / "templates/codex-hindsight-relay.example.toml").read_bytes())
            with patch.object(guard, "ROOT", root), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(guard.main(), 0)


if __name__ == "__main__":
    unittest.main()
