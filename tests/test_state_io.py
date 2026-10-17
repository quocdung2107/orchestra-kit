"""Atomic writes + lock, `orc set` refuses state fields, `orc advance`, journal."""

import json
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

PY = sys.executable

PLAN = """
## Acceptance criteria
- AC-1: there is a button

## Decisions for the user
None

## Task
```yaml orc-tasks
- key: a
  title: Make the button
  owner: coder
  acceptance: [AC-1]
  write_scope: ["src/"]
```
"""


def cli(root, *args, stdin=None):
    r = subprocess.run([PY, str(KIT / "cli" / "orc"), "-C", str(root), *args],
                       input=stdin.encode("utf-8") if stdin else None, capture_output=True)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


def ok(root, *args, stdin=None):
    code, out, err = cli(root, *args, stdin=stdin)
    assert code == 0, f"orc {' '.join(args)} → {code}: {out}{err}"
    return out.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        oc.cmd_init(self.root, "p", "g")

    def tearDown(self):
        self.tmp.cleanup()

    def journal(self):
        path = oc.journal_path(self.root)
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestAtomicWrite(Base):
    def test_failed_write_keeps_old_file_and_no_tmp(self):
        path = self.root / "x.txt"
        oc.write_text(path, "old")
        with mock.patch("os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                oc.write_text(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])


class TestLock(Base):
    def test_parallel_new_task_no_lost_or_duplicate_ids(self):
        procs = [subprocess.Popen([PY, str(KIT / "cli" / "orc"), "-C", str(self.root), "new", "task",
                                   "--title", f"t{i}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(6)]
        outs = [p.communicate() for p in procs]
        self.assertTrue(all(p.returncode == 0 for p in procs), outs)
        ids = sorted(o[0].decode().strip() for o in outs)
        self.assertEqual(ids, [f"TASK-{i:04d}" for i in range(1, 7)])
        self.assertEqual(len(oc.load_registry(self.root, "task")), 6)
        self.assertFalse((self.root / ".orchestra/runtime/state.lock").exists())

    def test_waits_then_errors_on_held_lock_and_breaks_stale(self):
        lock = self.root / ".orchestra/runtime/state.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("999 x", encoding="utf-8")
        with mock.patch.object(oc, "LOCK_WAIT", 0.2):
            with self.assertRaises(oc.OrcError):
                with oc.state_lock(self.root):
                    pass
        with mock.patch.object(oc, "LOCK_STALE", 0.0):
            with oc.state_lock(self.root):
                self.assertTrue(lock.exists())
        self.assertFalse(lock.exists())


class TestSetAndAdvance(Base):
    def test_set_refuses_state_fields(self):
        t = ok(self.root, "new", "task", "--title", "x")
        before = oc.registry_path(self.root, "task").read_text(encoding="utf-8")
        for pair in ("status=done", "attempts=0", "failure_class=null", "needs_user=false",
                     "resume_status=ready", "level=0", "approved_by=user"):
            code, _, err = cli(self.root, "set", t, pair)
            self.assertEqual(code, 1, pair)
            self.assertIn("orc advance", err)
        self.assertEqual(oc.registry_path(self.root, "task").read_text(encoding="utf-8"), before)
        ok(self.root, "set", t, "summary=allowed")

    def test_advance_table(self):
        a = oc.new_task(self.root, title="a")["id"]
        b = oc.new_task(self.root, title="b", blocked_by=[a])["id"]
        with self.assertRaises(oc.OrcError):
            oc.advance(self.root, a, "done")
        with self.assertRaises(oc.OrcError):
            oc.advance(self.root, a, "in_review")
        with self.assertRaises(oc.OrcError):
            oc.advance(self.root, b, "dispatched")  # still waiting for a
        self.assertEqual(oc.advance(self.root, a, "dispatched")["status"], "dispatched")
        art = oc.new_artifact(self.root, "plan", a)["id"]
        oc.advance(self.root, art, "accepted")
        with self.assertRaises(oc.OrcError):
            oc.advance(self.root, art, "draft")
        d = oc.new_decision(self.root, "x")["id"]
        with self.assertRaises(oc.OrcError):  # a DEC becomes accepted only through `orc approve`
            oc.advance(self.root, d, "accepted")
        with self.assertRaises(oc.OrcError):
            oc.advance(self.root, d, "proposed")

    def test_blocked_by_user_can_resume_but_not_blocked_by_tasks(self):
        t = oc.new_task(self.root, title="r", owner="researcher")["id"]
        oc.advance(self.root, t, "dispatched")
        oc.apply_envelope(self.root, oc.parse_envelope(
            f"msg: result\ntask_id: {t}\nstatus: blocked\nsummary: stuck\n"))
        e = oc.advance(self.root, t, "ready")
        self.assertEqual((e["status"], e["needs_user"]), ("ready", False))
        u = oc.new_task(self.root, title="u", blocked_by=[t])["id"]
        oc.set_fields(self.root, u, {"status": "blocked"})
        with self.assertRaises(oc.OrcError):
            oc.advance(self.root, u, "ready")


class TestJournal(Base):
    def test_each_transition_one_line_and_tamper_detected(self):
        t = ok(self.root, "new", "task", "--title", "x", "--owner", "researcher")
        ok(self.root, "advance", t, "--to", "dispatched")
        ok(self.root, "apply", "-", stdin=f"msg: result\ntask_id: {t}\nstatus: done\nsummary: ok\n"
                                           "evidence: [{kind: other, result: info, summary: n}]\n")
        rows = self.journal()
        self.assertEqual([(r["id"], r["from"], r["to"], r["actor"]) for r in rows], [
            (t, None, "pending", "orc new"), (t, "pending", "dispatched", "orc advance"),
            (t, "dispatched", "done", "orc apply")])
        self.assertEqual(rows[2]["evidence"], ["EVD-0001"])
        self.assertEqual(ok(self.root, "validate"), "OK")
        path = oc.journal_path(self.root)
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace('"dispatched"', '"done"')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        code, out, _ = cli(self.root, "validate")
        self.assertEqual(code, 1)
        self.assertIn("hash chain broken", out)


class TestV01StillRuns(Base):
    def test_full_loop_via_cli(self):
        r = self.root
        pt = ok(r, "new", "task", "--title", "Plan", "--owner", "researcher")
        art = ok(r, "new", "artifact", "--type", "plan", "--task", pt)
        path = oc.content_path(r, art)
        fm, _ = oc.split_frontmatter(path.read_text(encoding="utf-8"))
        oc.write_text(path, oc.join_frontmatter(fm, PLAN))
        ok(r, "advance", pt, "--to", "dispatched")
        ok(r, "apply", "-", stdin=f"msg: result\ntask_id: {pt}\nstatus: done\noutputs: [{art}]\nsummary: plan\n")
        self.assertEqual(ok(r, "gate", art), "PROCEED")
        ok(r, "advance", art, "--to", "accepted")
        t = ok(r, "new", "task", "--from", art)
        self.assertIn(f"NEXT: {t}", ok(r, "next"))
        ok(r, "advance", t, "--to", "dispatched")
        out = ok(r, "apply", "-", stdin=f"msg: result\ntask_id: {t}\nstatus: done\noutputs: [src/a.ts]\n"
                                         "evidence: [{kind: test, result: pass, summary: t}]\nsummary: code\n")
        self.assertIn("in_review", out)
        out = ok(r, "apply", "-", stdin=f"msg: result\ntask_id: {t}\nstatus: done\nsummary: passed\n")
        self.assertIn("→ done", out)
        self.assertEqual(ok(r, "validate"), "OK")
        self.assertIn("All tasks done", ok(r, "report", pt))


if __name__ == "__main__":
    unittest.main()
