"""State machine K — the root task follows K; coder tasks have approval + verify gates, I2, resume/abort."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KIT = REPO / "plugins" / "orchestra"  # plugin root (cli/, hooks/, templates/…)
sys.path.insert(0, str(KIT / "cli"))
import orc_core as oc  # noqa: E402
import orc_guard as og  # noqa: E402

PY = Path(sys.executable).as_posix()
UND_BODY = """
## My understanding of the request
Add a button.

## Preliminary design
One button.

## Unclear / missing information
None

## Questions for the user
Q1: blue? — Recommended: yes
"""
PLAN_BODY = """
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
  write_scope: ["app/"]
```
"""


def cli(root, *args, stdin=None):
    r = subprocess.run([sys.executable, str(KIT / "cli" / "orc"), "-C", str(root), *args], capture_output=True,
                       input=stdin.encode("utf-8") if stdin else None)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = r = Path(self.tmp.name)
        oc.cmd_init(r, "p", "g")
        with open(r / ".orchestra/config.yaml", "a", encoding="utf-8") as f:
            f.write(f"workflow: k\nverify:\n  commands:\n    test: '\"{PY}\" check.py'\n")
        (r / "check.py").write_text(
            "import sys\ns = open('app/status.txt', encoding='utf-8').read().strip()\n"
            "print('An Application Control policy has blocked this file' if s == 'env' else '# pass 1')\n"
            "sys.exit(0 if s == 'pass' else 1)\n", encoding="utf-8")
        (r / "app").mkdir()
        (r / "app/status.txt").write_text("pass\n", encoding="utf-8")
        git(r, "init", "-q")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def ok(self, *args, stdin=None):
        code, out, err = cli(self.root, *args, stdin=stdin)
        self.assertEqual(code, 0, f"orc {' '.join(args)}: {out}{err}")
        return out.strip()

    def no(self, *args, stdin=None):
        code, out, err = cli(self.root, *args, stdin=stdin)
        self.assertNotEqual(code, 0, f"orc {' '.join(args)} should have been refused: {out}")
        return err

    def say(self, text):
        og.log_prompt({"cwd": str(self.root), "session_id": "user", "prompt": text})

    def status(self, id_):
        return oc.find_entry(self.root, id_)[2]["status"]

    def fill(self, art, body):
        p = oc.content_path(self.root, art)
        oc.write_text(p, oc.join_frontmatter(oc.split_frontmatter(oc.read_text(p))[0], body))

    def env(self, tid, status="done", fc=None, outputs=()):
        return (f"msg: result\ntask_id: {tid}\nstatus: {status}\nsummary: x\noutputs: {json.dumps(list(outputs))}\n"
                + (f"failure_class: {fc}\n" if fc else ""))

    def to_planned(self):
        """request → … → PLANNED; returns (R, UND, DEC, PLAN, C)."""
        R = self.ok("new", "request", "--title", "Add a button", "--summary", "s")
        self.assertEqual(self.status(R), "NEW")
        self.ok("advance", R, "--to", "TRIAGED")
        self.no("advance", R, "--to", "PLANNED")  # the understanding stage always stops: no TRIAGED → PLANNED
        self.ok("advance", R, "--to", "RESEARCHING")
        UT = self.ok("new", "task", "--title", "Understand the request", "--owner", "researcher", "--parent", R)
        UND = self.ok("new", "artifact", "--type", "research", "--task", UT)
        self.fill(UND, UND_BODY)
        self.ok("advance", UT, "--to", "dispatched")
        self.ok("apply", "-", stdin=self.env(UT, outputs=[UND]))
        DEC = self.ok("new", "decision", "--scope", UND, "--summary", "Q1: blue")
        self.no("advance", R, "--to", "PLANNED")  # no plan yet
        PT = self.ok("new", "task", "--title", "Plan", "--owner", "researcher", "--parent", R)
        PLAN = self.ok("new", "artifact", "--type", "plan", "--task", PT)
        self.fill(PLAN, PLAN_BODY)
        self.ok("advance", PT, "--to", "dispatched")
        self.ok("apply", "-", stdin=self.env(PT, outputs=[PLAN]))
        self.assertEqual(self.ok("gate", PLAN), "PROCEED")
        self.ok("advance", PLAN, "--to", "accepted")
        C = self.ok("new", "task", "--from", PLAN)
        self.assertEqual(oc.find_entry(self.root, C)[2]["parent"], R)
        self.ok("advance", R, "--to", "PLANNED")
        return R, UND, DEC, PLAN, C

    def approve(self, R):
        self.no("advance", R, "--to", "READY_FOR_IMPLEMENTATION")  # no level yet
        out = self.ok("advance", R, "--to", "APPROVAL_REQUIRED")
        line = next(l.strip() for l in out.splitlines() if l.strip().startswith("orc:approve"))
        ids = line.split()[1].split(",")
        self.no("advance", R, "--to", "IMPLEMENTING")  # no prompt from the user yet
        self.say(line)
        self.ok("approve", *ids)
        self.ok("advance", R, "--to", "IMPLEMENTING")
        return ids

    def implement(self, C, status_txt="pass"):
        self.ok("next")
        self.ok("advance", C, "--to", "dispatched")
        (self.root / "app/status.txt").write_text(status_txt + "\n", encoding="utf-8")
        (self.root / "app/button.ts").write_text("export const b = 1\n", encoding="utf-8")
        self.ok("apply", "-", stdin=self.env(C, outputs=["app/button.ts"]))
        self.assertEqual(self.status(C), "in_review")


class TestKFullLoop(Base):
    def test_request_to_integrated(self):
        R, UND, DEC, PLAN, C = self.to_planned()
        self.ok("next")
        self.no("advance", C, "--to", "dispatched")  # not approved yet
        ids = self.approve(R)
        self.assertEqual(ids, [R, UND, DEC])  # one approval covers root + research + DEC
        self.assertEqual((self.status(UND), self.status(DEC)), ("accepted", "accepted"))
        self.implement(C)
        err = self.no("apply", "-", stdin=self.env(C))  # verify has not run since the submission
        self.assertIn("orc verify", err)
        rows = [json.loads(l) for l in oc.journal_path(self.root).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[-1]["event"], "rejected")
        self.ok("verify", C)
        self.no("advance", R, "--to", "DONE")
        self.ok("advance", R, "--to", "VERIFYING")
        self.ok("apply", "-", stdin=self.env(C))
        self.assertEqual(self.status(C), "done")
        self.ok("advance", R, "--to", "DONE")
        self.ok("advance", R, "--to", "INTEGRATED")
        self.assertEqual(self.ok("validate"), "OK")
        self.assertNotIn(R, oc.brief_text(self.root).split("## Open tasks")[1].split("##")[0])  # closed


class TestKGates(Base):
    def test_changes_after_approval_block_dispatch(self):
        R, UND, DEC, PLAN, C = self.to_planned()
        self.approve(R)
        p = oc.content_path(self.root, PLAN)
        oc.write_text(p, oc.read_text(p).replace("there is a button", "there is a red button"))
        self.ok("next")
        err = self.no("advance", C, "--to", "dispatched")
        self.assertIn("APPROVAL_MISSING", err)

    def test_k_project_requires_parent(self):
        err = self.no("new", "task", "--title", "orphan")
        self.assertIn("--parent", err)
        self.ok("new", "request", "--title", "root")

    def test_set_cannot_touch_k_fields(self):
        R = self.ok("new", "request", "--title", "root")
        for pair in ("status=DONE", "workflow=null", "parent=TASK-0001", "recovery_action=REPAIR"):
            self.no("set", R, pair)

    def test_next_shows_k_hint(self):
        R = self.ok("new", "request", "--title", "root")
        self.assertIn(f"K: {R} [NEW] → orc advance {R} --to TRIAGED", self.ok("next"))


class TestKRecovery(Base):
    def setUp(self):
        super().setUp()
        self.R, self.UND, self.DEC, self.PLAN, self.C = self.to_planned()
        self.approve(self.R)

    def test_verify_fail_repairs(self):
        self.implement(self.C, "fail")
        self.assertEqual(cli(self.root, "verify", self.C)[0], 6)  # a real FAIL
        out = self.ok("apply", "-", stdin=self.env(self.C))
        self.assertIn("REPAIR TEST_FAILURE", out)
        t = oc.find_entry(self.root, self.C)[2]
        self.assertEqual((t["status"], t["attempts"], t["recovery_action"]), ("ready", 1, "REPAIR"))
        self.assertEqual(self.status(self.R), "IMPLEMENTING")

    def test_environment_failure_escalates_and_resume_needs_user(self):
        self.implement(self.C, "env")
        cli(self.root, "verify", self.C)
        out = self.ok("apply", "-", stdin=self.env(self.C))
        self.assertIn("ESCALATE ENVIRONMENT_FAILURE", out)
        self.assertEqual((self.status(self.C), self.status(self.R)), ("blocked", "BLOCKED"))
        self.no("advance", self.C, "--to", "ready")       # a child task cannot unblock itself
        self.no("resume", self.R)                         # no prompt from the user yet
        self.say(f"orc:resume {self.R}")
        self.ok("resume", self.R)
        self.assertEqual((self.status(self.R), self.status(self.C)), ("IMPLEMENTING", "ready"))

    def test_scope_violation_rolls_back_and_escalates(self):
        self.ok("next")
        self.ok("advance", self.C, "--to", "dispatched")
        (self.root / "outside.txt").write_text("x\n", encoding="utf-8")
        self.ok("apply", "-", stdin=self.env(self.C, outputs=["app/status.txt"]))
        cli(self.root, "verify", self.C)
        out = self.ok("apply", "-", stdin=self.env(self.C))
        self.assertIn("ROLLBACK + ESCALATE SCOPE_VIOLATION", out)
        self.assertIn("git -C . diff", out)
        self.assertEqual(oc.find_entry(self.root, self.C)[2]["recovery_action"], "ROLLBACK")
        self.assertEqual(self.status(self.R), "BLOCKED")

    def test_review_failure_i2(self):
        self.implement(self.C)
        out = self.ok("apply", "-", stdin=self.env(self.C, "failed", "IMPL_BUG"))
        self.assertIn("REPAIR IMPL_BUG", out)
        self.implement(self.C)
        out = self.ok("apply", "-", stdin=self.env(self.C, "failed", "SPEC_GAP"))  # no level yet ⇒ ESCALATE
        self.assertIn("ESCALATE SPEC_GAP", out)
        self.assertEqual(self.status(self.R), "BLOCKED")

    def test_abort_needs_user(self):
        self.no("abort", self.R)
        self.say(f"orc:abort {self.R}")
        self.ok("abort", self.R)
        self.assertEqual(self.status(self.R), "ABORTED")


class TestAbortAnyTask(unittest.TestCase):
    """`orc abort` also cancels ordinary (non-K) tasks, not only K root tasks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        oc.cmd_init(self.root, "p", "g")

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_task_abort_needs_user_and_leaves_brief(self):
        t = oc.new_task(self.root, title="Build, test, lint (duplicate)", owner="coder")["id"]
        oc.next_report(self.root)  # pending → ready
        code, _, err = cli(self.root, "abort", t)
        self.assertEqual(code, 1)
        self.assertIn(f"orc:abort {t}", err)
        og.log_prompt({"cwd": str(self.root), "session_id": "user", "prompt": f"drop the duplicate task\norc:abort {t}"})
        code, out, err = cli(self.root, "abort", t)
        self.assertEqual(code, 0, err)
        self.assertEqual(oc.find_entry(self.root, t)[2]["status"], "aborted")
        self.assertIn("Open tasks (0)", oc.brief_text(self.root))
        self.assertNotIn(f"READY: {t}", oc.next_report(self.root))
        self.assertEqual(cli(self.root, "abort", t)[0], 1)  # already closed
        self.assertEqual(oc.validate(self.root), [])

    def test_approve_ignores_copied_nonce(self):
        import orc_approval as oa  # noqa: PLC0415
        d = oc.new_decision(self.root, "Q1")["id"]
        line = next(l.strip() for l in oa.request(self.root, [d]).splitlines() if "orc:approve" in l)
        og.log_prompt({"cwd": str(self.root), "session_id": "user", "prompt": line})
        nonce = line.split()[-1]
        code, _, err = cli(self.root, "approve", f"{d},{nonce}")
        self.assertEqual(code, 0, err)
        self.assertEqual(oc.find_entry(self.root, d)[2]["status"], "accepted")


class TestRecoveryTable(unittest.TestCase):
    def test_i2(self):
        import orc_k  # noqa: PLC0415
        cases = [("TEST_FAILURE", None, 1, "REPAIR"), ("TEST_FAILURE", None, 3, "ESCALATE"),
                 ("TEST_FAILURE", 3, 1, "ESCALATE"), ("SCOPE_VIOLATION", 0, 1, "ROLLBACK"),
                 ("REGRESSION", 1, 1, "ROLLBACK"), ("SPEC_GAP", 1, 1, "REPAIR"), ("SPEC_GAP", None, 1, "ESCALATE"),
                 ("ENVIRONMENT_FAILURE", 0, 1, "ESCALATE"), ("DECISION_CONFLICT", 0, 1, "ESCALATE"),
                 ("INTERNAL", 0, 1, "ABORT")]
        for fc, level, attempts, want in cases:
            with self.subTest(fc=fc, level=level, attempts=attempts):
                self.assertEqual(orc_k.recovery(fc, level, attempts), want)


if __name__ == "__main__":
    unittest.main()
