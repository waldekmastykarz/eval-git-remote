import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import submit_runs


class ConfigurationTests(unittest.TestCase):
    def test_api_scripts_require_explicit_owner(self):
        for script in ("import_definitions.py", "submit_runs.py"):
            with self.subTest(script=script):
                result = subprocess.run(
                    [sys.executable, str(ROOT / script), "--env-file", "unused",
                     "--prompt", "p1-push-optional", "--profiles", "all"]
                    if script == "submit_runs.py"
                    else [sys.executable, str(ROOT / script), "--env-file", "unused"],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("--owner", result.stderr)

    def test_submission_requires_local_import_state(self):
        self.check_submission(None, "Run import_definitions.py first")

    def test_submission_rejects_ids_from_another_api(self):
        self.check_submission(
            {"authority": "https://other.example.com"},
            "different API",
        )

    def check_submission(self, state, message):
        scratch = ROOT / ".tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            if state is not None:
                (Path(folder) / "imported.json").write_text(json.dumps(state))
            with (
                patch.object(submit_runs, "ROOT", Path(folder)),
                patch.object(sys, "argv", [
                    "submit_runs.py", "--env-file", "unused", "--owner",
                    "you@example.com", "--prompt", "p1-push-optional",
                    "--profiles", "all",
                ]),
                patch.object(submit_runs, "api_headers",
                             return_value=("https://api.example.com", {})),
                patch.object(submit_runs, "request") as request,
                self.assertRaisesRegex(SystemExit, message),
            ):
                submit_runs.main()
            request.assert_not_called()

    def test_github_setup_requires_owner_before_creating_resources(self):
        env = {**os.environ, "SCOPE_GITHUB_OWNER": ""}
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File",
             str(ROOT / "runner/setup.ps1"), "-Remote", "github"],
            env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set SCOPE_GITHUB_OWNER", result.stderr)

    def test_generated_templates_use_current_branch(self):
        profiles = list((ROOT / "profiles").glob("*.json"))
        self.assertEqual(len(profiles), 10)
        for path in profiles:
            with self.subTest(profile=path.name):
                profile = json.loads(path.read_text())
                self.assertEqual(profile["metadata"]["runnerRef"], "main")
                for script in profile["scripts"].values():
                    self.assertIn("/main/runner/", script)

    def test_environment_state_is_ignored(self):
        for path in ("imported.json", ".env", ".env.local", ".tmp/check"):
            with self.subTest(path=path):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT,
                )
                self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
