"""scripts/py: picks python3 / python / py -3, caches the choice, and fails soft in hooks without Python.

Run: python -m unittest tests.test_launcher -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KIT = REPO / "plugins" / "orcgate"  # plugin root (cli/, hooks/, templates/…)
SH = shutil.which("sh")


@unittest.skipUnless(SH and os.name != "nt", "needs a POSIX sh")
class TestLauncher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"  # PATH with the basic tools and no Python unless a test adds one
        self.bin.mkdir()
        for tool in ("sh", "cat", "dirname", "mkdir", "env"):
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        self.data = self.dir / "data"
        self.project = self.dir / "proj"
        (self.project / ".orchestra").mkdir(parents=True)
        (self.project / ".orchestra" / "config.yaml").write_text("project_id: p\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def add_python(self, name):
        (self.bin / name).symlink_to(sys.executable)

    def run_py(self, *args, cwd=None, extra_env=None):
        env = {"PATH": str(self.bin), "CLAUDE_PLUGIN_DATA": str(self.data)}
        env.update(extra_env or {})
        r = subprocess.run([SH, str(KIT / "scripts" / "py"), *args], cwd=cwd or self.dir, env=env,
                           input=b"{}", capture_output=True)
        return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")

    def test_only_python3_runs_orc_and_caches_it(self):
        self.add_python("python3")
        code, out, err = self.run_py(str(KIT / "cli" / "orc"), "--help")
        self.assertEqual(code, 0, err)
        self.assertIn("orcgate CLI", out)
        self.assertEqual((self.data / "python-cmd").read_text(encoding="utf-8").splitlines()[0], "python3")

    def test_only_python_runs(self):
        self.add_python("python")
        code, out, err = self.run_py(str(KIT / "cli" / "orc"), "--help")
        self.assertEqual(code, 0, err)

    def test_stale_cache_is_probed_again(self):
        self.data.mkdir()
        (self.data / "python-cmd").write_text("python9\n\n", encoding="utf-8")
        self.add_python("python3")
        code, _, err = self.run_py(str(KIT / "cli" / "orc"), "--help")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.data / "python-cmd").read_text(encoding="utf-8").splitlines()[0], "python3")

    def test_override_with_orcgate_python(self):
        code, _, err = self.run_py(str(KIT / "cli" / "orc"), "--help", extra_env={"ORCGATE_PYTHON": sys.executable})
        self.assertEqual(code, 0, err)

    def test_store_alias_that_fails_is_skipped(self):
        stub = self.bin / "python"
        stub.write_text("#!/bin/sh\necho 'Python was not found' >&2\nexit 9009\n", encoding="utf-8")
        stub.chmod(0o755)
        self.add_python("python3")
        code, _, err = self.run_py(str(KIT / "cli" / "orc"), "--help")
        self.assertEqual(code, 0, err)

    def test_no_python_cli_fails_loudly(self):
        code, _, err = self.run_py(str(KIT / "cli" / "orc"), "--help")
        self.assertEqual(code, 127)
        self.assertIn("Python 3.10+ not found", err)

    def test_no_python_hook_warns_only_in_orcgate_projects(self):
        code, out, _ = self.run_py("--hook", str(KIT / "hooks" / "session_start"), cwd=self.project / ".orchestra")
        self.assertEqual(code, 0)
        self.assertIn("Python 3.10+ not found", out)
        elsewhere = self.dir / "other"
        elsewhere.mkdir()
        code, out, _ = self.run_py("--hook", str(KIT / "hooks" / "session_start"), cwd=elsewhere)
        self.assertEqual((code, out), (0, ""))

    def test_hook_gets_stdin(self):
        self.add_python("python3")
        env = {"PATH": str(self.bin), "CLAUDE_PLUGIN_DATA": str(self.data)}
        payload = json.dumps({"cwd": str(self.dir / "nowhere"), "session_id": "x"}).encode("utf-8")
        r = subprocess.run([SH, str(KIT / "scripts" / "py"), "--hook", str(KIT / "hooks" / "session_start")],
                           env=env, input=payload, capture_output=True)
        self.assertEqual((r.returncode, r.stdout), (0, b""), r.stderr)

    def test_bin_orc_uses_the_launcher(self):
        self.add_python("python3")
        r = subprocess.run([str(KIT / "bin" / "orc"), "--help"], env={"PATH": str(self.bin)}, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
