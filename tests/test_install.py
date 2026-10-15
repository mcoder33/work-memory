import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import install as installer
import work_memory


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.vault = self.root / "vault with spaces"
        self.vault.mkdir()
        (self.home / ".codex/sessions").mkdir(parents=True)
        self.config = self.home / ".config/work-memory/config.json"
        self.agents = self.home / ".codex/AGENTS.md"

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, *flags, repo=None):
        argv = ["install.py", "--vault", str(self.vault), "--since", "2026-10-16", *flags]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(Path, "home", return_value=self.home))
            stack.enter_context(patch.object(work_memory, "HOME", self.home))
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if repo:
                stack.enter_context(patch.object(installer, "__file__", str(repo / "install.py")))
            return installer.install()

    def test_clean_install_creates_rules_without_private_project_aliases(self):
        self.assertEqual(self.run_install("--agent-instructions"), 0)
        settings = json.loads(self.config.read_text())
        self.assertEqual(settings["projects"], {})
        self.assertEqual(settings["vault"], str(self.vault.resolve()))
        self.assertTrue(self.agents.exists())
        self.assertIn("Общая память работы в Obsidian", self.agents.read_text())
        self.assertIn(str(self.vault.resolve()), self.agents.read_text())

    def test_reinstall_preserves_rules_and_existing_settings(self):
        self.agents.write_text("Existing user rule.\n")
        self.run_install("--agent-instructions")
        settings = json.loads(self.config.read_text())
        settings["projects"] = {"/projects/example": "example"}
        self.config.write_text(json.dumps(settings))
        before = self.agents.read_bytes()
        self.run_install("--agent-instructions")
        self.assertEqual(self.agents.read_bytes(), before)
        self.assertTrue(self.agents.read_text().startswith("Existing user rule."))
        self.assertEqual(json.loads(self.config.read_text())["projects"], settings["projects"])
        backups = list((self.home / ".local/share/work-memory").glob("AGENTS-before-memory-*.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "Existing user rule.\n")

    def test_installed_wrapper_handles_clone_and_home_paths_with_spaces(self):
        repo = self.root / "clone with spaces"
        repo.mkdir()
        shutil.copyfile(Path(work_memory.__file__), repo / "work_memory.py")
        self.run_install(repo=repo)
        cli = self.home / ".local/bin/work-memory"
        result = subprocess.run([str(cli), "--config", str(self.config), "status"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pending_writes"], 0)

    def test_unsupported_service_fails_before_changing_files(self):
        with patch.object(sys, "platform", "linux"):
            with self.assertRaisesRegex(ValueError, "macOS"):
                self.run_install("--service")
        self.assertFalse(self.config.exists())
        self.assertFalse((self.home / ".local/bin/work-memory").exists())


if __name__ == "__main__":
    unittest.main()
