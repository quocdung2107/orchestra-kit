"""orc verify (V1/V2) + checkpoint + scope via git."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
KIT = REPO / "plugins" / "orchestra"  # plugin root (cli/, hooks/, templates/…)
sys.path.insert(0, str(KIT / "cli"))
import orc_core as oc  # noqa: E402
import orc_verify as ov  # noqa: E402

PY = Path(sys.executable).as_posix()


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def cli(root, *args):
    r = subprocess.run([sys.executable, str(KIT / "cli" / "orc"), "-C", str(root), *args], capture_output=True)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


VERIFY_CFG = f"""
verify:
  timeout: 30
  commands:
    test: {{run: '"{PY}" check.py', cwd: app}}
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = r = Path(self.tmp.name)
        oc.cmd_init(r, "p", "g")
        with open(r / ".orchestra/config.yaml", "a", encoding="utf-8") as f:
            f.write(VERIFY_CFG)
        (r / "app").mkdir()
        (r / "app/check.py").write_text("import sys; print('# pass 3'); print('# fail 0')\n", encoding="utf-8")
        (r / "app/a.py").write_text("a = 1\n", encoding="utf-8")
        (r / "app/package.json").write_text("{}\n", encoding="utf-8")
        (r / ".gitignore").write_text("out/\n", encoding="utf-8")
        (r / "wip.txt").write_text("the user's work in progress\n", encoding="utf-8")
        git(r, "init", "-q")
        git(r, "add", "-A", ":!wip.txt")
        git(r, "commit", "-q", "-m", "init")
        (r / "wip.txt").write_text("the user's work in progress, not committed\n", encoding="utf-8")  # dirty before the checkpoint
        self.t = oc.new_task(r, title="Code", owner="coder", write_scope=["app/a.py", "app/new.py"])["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def dispatch(self):
        code, out, err = cli(self.root, "advance", self.t, "--to", "dispatched")
        self.assertEqual(code, 0, out + err)

    def verify(self):
        code, out, err = cli(self.root, "verify", self.t)
        return code, out + err


class TestCheckpoint(Base):
    def test_dispatch_creates_checkpoint_bound_to_journal(self):
        self.dispatch()
        cp = ov.checkpoint_path(self.root, self.t)
        self.assertTrue(cp.is_file())
        data = json.loads(cp.read_text(encoding="utf-8"))
        self.assertIn("wip.txt", data["repos"]["."]["dirty"])
        rows = [json.loads(l) for l in oc.journal_path(self.root).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(r["id"] == self.t and r.get("checkpoint") for r in rows))

    def test_commands_come_from_checkpoint_not_current_config(self):
        self.dispatch()
        cfg = self.root / ".orchestra/config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace("check.py", "evil.py"), encoding="utf-8")
        code, out = self.verify()
        self.assertEqual(code, 0, out)
        self.assertIn("check.py", oc.read_text(next((self.root / ".orchestra/evidence").glob("EVD-0002.md"))))

    def test_tampered_checkpoint_refused(self):
        self.dispatch()
        cp = ov.checkpoint_path(self.root, self.t)
        cp.write_text(cp.read_text(encoding="utf-8").replace("check.py", "evil.py"), encoding="utf-8")
        code, out = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("does not match the journal", out)

    def test_no_verify_config_keeps_v01_behaviour(self):
        with tempfile.TemporaryDirectory() as d:
            oc.cmd_init(Path(d), "p", "g")
            t = oc.new_task(Path(d), title="x", owner="coder")["id"]
            self.assertEqual(oc.advance(Path(d), t, "dispatched")["status"], "dispatched")
            self.assertFalse(ov.checkpoint_path(Path(d), t).exists())


class TestScope(Base):
    def test_clean_change_in_scope_passes_and_wip_ignored(self):
        self.dispatch()
        (self.root / "app/a.py").write_text("a = 2\n", encoding="utf-8")
        (self.root / "app/new.py").write_text("b = 1\n", encoding="utf-8")
        code, out = self.verify()
        self.assertEqual(code, 0, out)
        self.assertIn("VERIFY TASK-0001: PASS", out)
        evd = oc.find_entry(self.root, "EVD-0002")[2]
        self.assertEqual((evd["kind"], evd["result"], evd["by"]), ("test", "pass", "orc"))
        self.assertIn("pass 3/fail 0", evd["summary"])

    def test_outside_scope_tracked_untracked_ignored_and_wip_change(self):
        self.dispatch()
        (self.root / "app/check.py").write_text("print('# pass 1')\n", encoding="utf-8")   # tracked
        (self.root / "stray.txt").write_text("x\n", encoding="utf-8")                      # untracked
        (self.root / "out").mkdir()
        (self.root / "out/hidden.txt").write_text("x\n", encoding="utf-8")                 # new ignored entry
        (self.root / "wip.txt").write_text("overwrites the user's work in progress\n", encoding="utf-8")     # dirty before, then changed
        git(self.root, "add", "app/check.py")
        git(self.root, "commit", "-q", "-m", "a commit does not slip through either")
        code, out = self.verify()
        self.assertEqual(code, 6, out)
        for f in ("app/check.py", "stray.txt", "out/", "wip.txt"):
            self.assertIn(f, out)

    def test_limit_writes_inside_existing_ignored_dir_not_detected(self):
        # `git status --ignored` groups by directory ⇒ more writes into an already ignored directory are not visible.
        (self.root / "out").mkdir()
        (self.root / "out/old.txt").write_text("x\n", encoding="utf-8")
        self.dispatch()
        (self.root / "out/new.txt").write_text("slips through\n", encoding="utf-8")
        code, out = self.verify()
        self.assertEqual(code, 0, out)
        self.assertNotIn("out/", out)

    def test_secret_detected(self):
        self.dispatch()
        (self.root / "app/a.py").write_text('API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123"\n', encoding="utf-8")
        code, out = self.verify()
        self.assertEqual(code, 6)
        self.assertIn("Suspected secrets", out)

    def test_package_json_change_blocks_commands(self):
        self.dispatch()
        (self.root / "app/package.json").write_text('{"scripts": {"test": "evil"}}\n', encoding="utf-8")
        code, out = self.verify()
        self.assertEqual(code, 6)
        self.assertIn("Commands not run because these changed: app/package.json", out)
        self.assertIn("risk_paths", out)
        self.assertFalse((self.root / ".orchestra/runtime/verify").exists())


class TestExecutionEnvironment(Base):
    def test_env_strips_secret_names(self):
        with mock.patch.dict(os.environ, {"SUPABASE_SERVICE_KEY": "x", "GH_TOKEN": "y", "MY_PASSWORD": "z",
                                          "HARMLESS": "ok"}):
            env = ov.safe_env()
        self.assertNotIn("SUPABASE_SERVICE_KEY", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("MY_PASSWORD", env)
        self.assertEqual(env["HARMLESS"], "ok")
        self.assertIn("PATH", {k.upper() for k in env})

    def test_child_does_not_see_secret_env(self):
        logdir = self.root / "logs"
        with mock.patch.dict(os.environ, {"GH_TOKEN": "secret-value"}):
            res = ov.run_command(self.root, "test", {"run": f'"{PY}" -c "import os; print(os.environ.get(\'GH_TOKEN\'))"',
                                                     "cwd": ".", "timeout": 30}, logdir)
        self.assertIn("None", (logdir / "test.log").read_text(encoding="utf-8"))
        self.assertEqual(res["result"], "pass")

    def test_cwd_outside_root_refused(self):
        with self.assertRaises(oc.OrcError):
            ov.run_command(self.root, "test", {"run": "echo x", "cwd": "..", "timeout": 5}, self.root / "l")

    def test_timeout_kills(self):
        res = ov.run_command(self.root, "test", {"run": f'"{PY}" -c "import time; time.sleep(30)"',
                                                 "cwd": ".", "timeout": 1}, self.root / "l")
        self.assertTrue(res["timed_out"])
        self.assertEqual(res["result"], "fail")
        self.assertLess(res["seconds"], 15)


class TestCompare(Base):
    def test_false_self_declared_pass_is_flagged(self):
        self.dispatch()
        (self.root / "app/check.py").write_text("import sys; sys.exit(1)\n", encoding="utf-8")
        git(self.root, "add", "app/check.py")  # changed first so the mismatch shows in the test, not only in the scope
        env = (f"msg: result\ntask_id: {self.t}\nstatus: done\noutputs: [app/a.py]\n"
               "evidence: [{kind: test, result: pass, summary: 'npm test: 3/3 pass'}]\nsummary: xong\n")
        oc.apply_envelope(self.root, oc.parse_envelope(env))
        code, out = self.verify()
        self.assertEqual(code, 6)
        self.assertIn("- test: orc fail | self-reported pass ['EVD-0001'] | MISMATCH", out)

    def test_envelope_cannot_claim_orc_only_kinds(self):
        self.dispatch()
        env = (f"msg: result\ntask_id: {self.t}\nstatus: done\n"
               "evidence: [{kind: scope, result: pass, summary: 'scope ok'}]\nsummary: xong\n")
        with self.assertRaises(oc.OrcError):
            oc.apply_envelope(self.root, oc.parse_envelope(env))

    def test_validate_ok_and_pack_shows_by(self):
        self.dispatch()
        self.verify()
        self.assertEqual(oc.validate(self.root), [])
        self.assertEqual(oc.find_entry(self.root, self.t)[2]["outputs"], ["EVD-0001", "EVD-0002"])
        self.assertIn("EVD-0002: kind=test result=pass by=orc", oc.show(self.root, self.t, pack=True))


if __name__ == "__main__":
    unittest.main()
