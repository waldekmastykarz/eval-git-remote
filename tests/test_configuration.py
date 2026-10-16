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

    def test_github_setup_requires_gh_login_before_creating_resources(self):
        scratch = ROOT / ".tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as config:
            # An empty gh config and no token env vars: gh isn't signed in.
            env = {k: v for k, v in os.environ.items()
                   if k not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")}
            env["GH_CONFIG_DIR"] = config
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File",
                 str(ROOT / "runner/setup.ps1"), "-Remote", "github"],
                env=env, capture_output=True, text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gh CLI signed in", result.stderr)

    def test_github_setup_uses_signed_in_gh_account(self):
        scratch = ROOT / ".tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            folder = Path(folder)
            fake_bin = folder / "bin"
            fake_bin.mkdir()
            # Stand-in gh: reports a signed-in account and fails everything else, so setup stops
            # at its first GitHub call and shows which owner it used.
            fake_gh = fake_bin / "gh"
            fake_gh.write_text('#!/bin/sh\n[ "$1 $2" = "api user" ] && { echo signed-in-account; exit 0; }\nexit 1\n')
            fake_gh.chmod(0o755)
            workspace = folder / "run" / "workspace"
            workspace.mkdir(parents=True)
            env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File",
                 str(ROOT / "runner/setup.ps1"), "-Remote", "github",
                 "-BundleUrl", str(ROOT / "fixture/baseline.bundle")],
                cwd=workspace, env=env, capture_output=True, text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gh repo list signed-in-account failed", result.stderr)

    def test_profiles_pin_all_assets_to_one_commit(self):
        profiles = list((ROOT / "profiles").glob("*.json"))
        self.assertEqual(len(profiles), 10)
        refs = set()
        for path in profiles:
            with self.subTest(profile=path.name):
                profile = json.loads(path.read_text())
                ref = profile["metadata"]["runnerRef"]
                self.assertRegex(ref, r"^[0-9a-f]{40}$")
                refs.add(ref)
                base = f"https://raw.githubusercontent.com/waldekmastykarz/eval-git-remote/{ref}"
                self.assertEqual(
                    profile["scripts"]["setup"],
                    f"& ([scriptblock]::Create((irm {base}/runner/setup.ps1))) "
                    f"-Remote {profile['metadata']['remote']} "
                    f"-BundleUrl {base}/fixture/baseline.bundle",
                )
                self.assertEqual(
                    profile["scripts"]["teardown"],
                    f"& ([scriptblock]::Create((irm {base}/runner/teardown.ps1)))",
                )
        self.assertEqual(len(refs), 1)
        for asset in ("runner/setup.ps1", "runner/teardown.ps1", "fixture/baseline.bundle"):
            result = subprocess.run(
                ["git", "cat-file", "-e", f"{ref}:{asset}"], cwd=ROOT,
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_environment_state_is_ignored(self):
        for path in ("imported.json", ".env", ".env.local", ".tmp/check"):
            with self.subTest(path=path):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT,
                )
                self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
