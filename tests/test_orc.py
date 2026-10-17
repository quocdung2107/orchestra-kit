"""Automated tests for orc. Run: python -m unittest discover -s tests -v"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KIT = REPO / "plugins" / "orchestra"  # plugin root (cli/, hooks/, templates/…)
sys.path.insert(0, str(KIT / "cli"))

import orc_core as oc  # noqa: E402

PY = sys.executable


def cli(root, *args, stdin=None):
    # stdin is sent as UTF-8 bytes, like a shell on Windows (independent of the code page)
    r = subprocess.run([PY, str(KIT / "cli" / "orc"), "-C", str(root), *args],
                       input=stdin.encode("utf-8") if stdin else None, capture_output=True)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


def hook(name, payload):
    r = subprocess.run([PY, str(KIT / "hooks" / name)], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout, r.stderr


PLAN_BODY = """
# Plan

## Acceptance criteria
- AC-1: the delete-data button works
- AC-2: a confirmation is shown before deleting

## Task
```yaml orc-tasks
- key: ui
  title: Add the delete button
  owner: coder
  acceptance: [AC-1, AC-2]
  write_scope: ["app/src/features/settings/"]
- key: doc
  title: Write the docs
  owner: coder
  acceptance: [AC-1]
  write_scope: ["docs/*.md"]
  blocked_by: [ui]
```
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        oc.cmd_init(self.root, "demo", "Try orchestra")

    def tearDown(self):
        self.tmp.cleanup()

    def plan_with_tasks(self):
        """Create a root task + an accepted plan + 2 child tasks; return (plan_task, art, [t_ui, t_doc])."""
        t = oc.new_task(self.root, title="Make a plan", owner="researcher")["id"]
        art = oc.new_artifact(self.root, "plan", t)["id"]
        path = oc.content_path(self.root, art)
        fm, _ = oc.split_frontmatter(path.read_text(encoding="utf-8"))
        oc.write_text(path, oc.join_frontmatter(fm, PLAN_BODY))
        oc.set_fields(self.root, art, {"status": "accepted"})
        kids = [e["id"] for e in oc.tasks_from_plan(self.root, art)]
        return t, art, kids

    def dispatch_and_finish(self, tid, outputs=("app/src/features/settings/Clear.tsx",)):
        oc.set_fields(self.root, tid, {"status": "dispatched"})
        env = {"msg": "result", "task_id": tid, "status": "done", "outputs": list(outputs),
               "evidence": [{"kind": "test", "result": "pass", "summary": "npm test ok"}],
               "proposed_decisions": [], "summary": "xong"}
        return oc.apply_envelope(self.root, oc.parse_envelope(oc.dump_yaml(env)))

    def review(self, tid, status="failed", fc=None):
        env = {"msg": "result", "task_id": tid, "status": status, "outputs": [],
               "evidence": [], "proposed_decisions": [], "summary": "review",
               "failure_class": fc}
        return oc.apply_envelope(self.root, oc.parse_envelope(oc.dump_yaml(env)))

    def task(self, tid):
        return oc.find_entry(self.root, tid)[2]


class TestInitAndIds(Base):
    def test_init_structure(self):
        o = self.root / ".orchestra"
        for d in ("state", "decisions", "artifacts", "contexts", "evidence", "sessions"):
            self.assertTrue((o / d).is_dir(), d)
        for f in ("tasks", "artifacts", "decisions", "contexts", "evidence"):
            self.assertTrue((o / "state" / f"{f}.yaml").is_file())
        self.assertTrue((o / "knowledge" / "KNOWLEDGE_MAP.md").is_file())
        cfg = oc.load_config(self.root)
        self.assertEqual(cfg["project_id"], "demo")
        self.assertEqual(cfg["kit_version"], (KIT / "VERSION").read_text().strip())
        self.assertEqual(oc.validate(self.root), [])

    def test_init_refuses_overwrite(self):
        with self.assertRaises(oc.OrcError):
            oc.cmd_init(self.root, "demo", "x")

    def test_init_keeps_existing_claude_md(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "CLAUDE.md").write_text("old", encoding="utf-8")
            oc.cmd_init(Path(d), "p", "g")
            self.assertEqual((Path(d) / "CLAUDE.md").read_text(encoding="utf-8"), "old")

    def test_sequential_ids_per_kind(self):
        self.assertEqual(oc.new_task(self.root, title="a")["id"], "TASK-0001")
        self.assertEqual(oc.new_task(self.root, title="b")["id"], "TASK-0002")
        self.assertEqual(oc.new_decision(self.root, "d")["id"], "DEC-0001")
        self.assertEqual(oc.new_artifact(self.root, "plan", "TASK-0001")["id"], "ART-0001")
        self.assertEqual(oc.handoff_open(self.root, "orchestrator"), "S-0001")
        self.assertEqual(oc.handoff_open(self.root, "orchestrator"), "S-0002")

    def test_cli_new_prints_only_id(self):
        code, out, err = cli(self.root, "new", "task", "--title", "Job 1")
        self.assertEqual((code, out.strip()), (0, "TASK-0001"), err)


class TestValidate(Base):
    def test_rejects_long_summary(self):
        with self.assertRaises(oc.OrcError):
            oc.new_task(self.root, title="x", summary="a" * 201)
        oc.new_task(self.root, title="x", summary="a" * 200)

    def test_rejects_duplicate_id(self):
        oc.new_task(self.root, title="x")
        items = oc.load_registry(self.root, "task")
        oc.save_registry(self.root, "task", items + [dict(items[0])])
        self.assertTrue(any("duplicate ID" in e for e in oc.validate(self.root)))

    def test_rejects_missing_reference(self):
        with self.assertRaises(oc.OrcError):
            oc.new_task(self.root, title="x", blocked_by=["TASK-0099"])
        t = oc.new_task(self.root, title="x")
        items = oc.load_registry(self.root, "task")
        items[0]["inputs"] = ["ART-0042"]
        oc.save_registry(self.root, "task", items)
        errs = oc.validate(self.root)
        self.assertTrue(any("ART-0042" in e and "does not exist" in e for e in errs), errs)
        self.assertEqual(t["id"], "TASK-0001")

    def test_rejects_bad_acceptance_anchor(self):
        _, art, _ = self.plan_with_tasks()
        with self.assertRaises(oc.OrcError):
            oc.new_task(self.root, title="x", acceptance=[f"{art}#AC-9"])
        with self.assertRaises(oc.OrcError):
            oc.new_task(self.root, title="x", acceptance=["AC-1"])

    def test_rejects_bad_frontmatter(self):
        t = oc.new_task(self.root, title="x")["id"]
        art = oc.new_artifact(self.root, "plan", t)["id"]
        path = oc.content_path(self.root, art)
        path.write_text("no frontmatter here", encoding="utf-8")
        self.assertTrue(any("missing frontmatter" in e for e in oc.validate(self.root)))
        path.write_text("---\nid: [\n---\nbody", encoding="utf-8")
        self.assertTrue(any("YAML" in e for e in oc.validate(self.root)))

    def test_rejects_frontmatter_registry_mismatch(self):
        t = oc.new_task(self.root, title="x")["id"]
        art = oc.new_artifact(self.root, "plan", t)["id"]
        path = oc.content_path(self.root, art)
        path.write_text(path.read_text(encoding="utf-8").replace("status: draft", "status: accepted"),
                        encoding="utf-8")
        self.assertTrue(any("differs from the registry" in e for e in oc.validate(self.root)))

    def test_rejects_bad_enum_and_orphan_file(self):
        oc.new_task(self.root, title="x")
        with self.assertRaises(oc.OrcError):
            oc.set_fields(self.root, "TASK-0001", {"status": "doing"})
        oc.write_text(self.root / ".orchestra/artifacts/ART-0007.md", "---\nid: ART-0007\n---\n")
        self.assertTrue(any("ART-0007" in e and "registry" in e for e in oc.validate(self.root)))

    def test_cli_validate_exit_code(self):
        self.assertEqual(cli(self.root, "validate")[0], 0)
        oc.write_text(self.root / ".orchestra/state/tasks.yaml",
                      "- id: TASK-0001\n  title: x\n  status: ready\n")
        code, out, _ = cli(self.root, "validate")
        self.assertEqual(code, 1)
        self.assertIn("missing fields", out)


class TestSetShow(Base):
    def test_set_artifact_syncs_frontmatter(self):
        t = oc.new_task(self.root, title="x")["id"]
        art = oc.new_artifact(self.root, "plan", t, summary="s")["id"]
        # status changes only through `orc advance`; `orc set` still edits ordinary fields
        code, _, err = cli(self.root, "advance", art, "--to", "accepted")
        self.assertEqual(code, 0, err)
        code, _, err = cli(self.root, "set", art, "summary=new")
        self.assertEqual(code, 0, err)
        fm, _ = oc.split_frontmatter(oc.content_path(self.root, art).read_text(encoding="utf-8"))
        self.assertEqual((fm["status"], fm["summary"]), ("accepted", "new"))
        self.assertEqual(oc.validate(self.root), [])

    def test_artifact_supersedes_bumps_version(self):
        t = oc.new_task(self.root, title="x")["id"]
        a1 = oc.new_artifact(self.root, "plan", t)["id"]
        a2 = oc.new_artifact(self.root, "plan", t, supersedes=a1)
        self.assertEqual((a2["version"], a2["supersedes"]), (2, a1))

    def test_decision_accept_supersedes_old(self):
        d1 = oc.new_decision(self.root, "old", status="accepted")["id"]
        d2 = oc.new_decision(self.root, "new", supersedes=d1)["id"]
        oc.set_fields(self.root, d2, {"status": "accepted"})
        self.assertEqual(oc.find_entry(self.root, d1)[2]["status"], "superseded")
        self.assertTrue((self.root / f".orchestra/decisions/{d2}.md").is_file())

    def test_show_meta_is_registry_only(self):
        t = oc.new_task(self.root, title="x")["id"]
        art = oc.new_artifact(self.root, "plan", t, summary="short summary")["id"]
        code, out, _ = cli(self.root, "show", art, "--meta")
        self.assertEqual(code, 0)
        self.assertIn("summary: short summary", out)
        self.assertNotIn("# Plan", out)
        self.assertIn("# Plan", cli(self.root, "show", art)[1])

    def test_pack_excludes_coder_summary(self):
        _, art, (ui, _) = self.plan_with_tasks()
        oc.promote(self.root)
        self.dispatch_and_finish(ui)
        oc.set_fields(self.root, ui, {"summary": "the coder's secret reasoning"})
        pack = oc.show(self.root, ui, pack=True)
        self.assertIn("AC-1: the delete-data button works", pack)
        self.assertIn("app/src/features/settings/Clear.tsx", pack)
        self.assertIn("EVD-0001", pack)
        self.assertNotIn("secret reasoning", pack)


class TestNextAndRouting(Base):
    def test_tasks_from_plan_requires_accepted(self):
        t = oc.new_task(self.root, title="Make a plan", owner="researcher")["id"]
        art = oc.new_artifact(self.root, "plan", t)["id"]
        with self.assertRaises(oc.OrcError):
            oc.tasks_from_plan(self.root, art)

    def test_next_respects_blocked_by(self):
        _, art, (ui, doc) = self.plan_with_tasks()
        self.assertEqual(self.task(doc)["blocked_by"], [ui])
        self.assertEqual(self.task(doc)["acceptance"], [f"{art}#AC-1"])
        report = oc.next_report(self.root)
        self.assertIn(f"NEXT: TASK-0001", report)  # the planning task is ready first
        self.assertEqual(self.task(ui)["status"], "ready")
        self.assertEqual(self.task(doc)["status"], "pending")
        self.dispatch_and_finish(ui)
        self.assertEqual(self.task(ui)["status"], "in_review")
        self.review(ui, status="done")
        oc.promote(self.root)
        self.assertEqual(self.task(doc)["status"], "ready")

    def test_coder_output_outside_write_scope_rejected(self):
        _, _, (ui, _) = self.plan_with_tasks()
        oc.promote(self.root)
        with self.assertRaises(oc.OrcError):
            self.dispatch_and_finish(ui, outputs=("app/src/main.tsx",))
        self.assertEqual(self.task(ui)["status"], "dispatched")  # nothing written
        self.assertEqual(oc.load_registry(self.root, "evidence"), [])

    def test_researcher_done_goes_done(self):
        t = oc.new_task(self.root, title="plan", owner="researcher")["id"]
        art = oc.new_artifact(self.root, "plan", t)["id"]
        oc.promote(self.root)
        oc.set_fields(self.root, t, {"status": "dispatched"})
        env = (f"```yaml\nmsg: result\ntask_id: {t}\nstatus: done\noutputs: [{art}]\n"
               "evidence: []\nproposed_decisions: ['Use SQLite']\nsummary: plan done\n```")
        code, out, err = cli(self.root, "apply", "-", stdin=env)
        self.assertEqual(code, 0, err)
        self.assertIn("→ done", out)
        dec = oc.find_entry(self.root, "DEC-0001")[2]
        self.assertEqual((dec["status"], dec["summary"]), ("proposed", "Use SQLite"))

    def test_impl_bug_returns_to_coder_then_asks_user(self):
        _, _, (ui, _) = self.plan_with_tasks()
        oc.promote(self.root)
        for attempt in (1, 2):
            self.dispatch_and_finish(ui)
            route = self.review(ui, fc="IMPL_BUG")
            self.assertEqual(self.task(ui)["status"], "ready")
            self.assertEqual(self.task(ui)["attempts"], attempt)
            self.assertIn("coder", route)
        self.dispatch_and_finish(ui)
        route = self.review(ui, fc="IMPL_BUG")
        t = self.task(ui)
        self.assertEqual((t["status"], t["attempts"], t["needs_user"]), ("blocked", 3, True))
        self.assertIn("ASK USER", route)
        oc.promote(self.root)
        self.assertEqual(self.task(ui)["status"], "blocked")

    def test_spec_gap_creates_replan_task(self):
        _, art, (ui, _) = self.plan_with_tasks()
        oc.promote(self.root)
        self.dispatch_and_finish(ui)
        route = self.review(ui, fc="SPEC_GAP")
        t = self.task(ui)
        fix = t["blocked_by"][-1]
        self.assertEqual(t["status"], "blocked")
        self.assertEqual(self.task(fix)["owner"], "researcher")
        self.assertIn(art, self.task(fix)["inputs"])
        self.assertIn("replan", route)
        # replan done → the root task is ready again
        oc.promote(self.root)
        oc.set_fields(self.root, fix, {"status": "done"})
        oc.promote(self.root)
        self.assertEqual(self.task(ui)["status"], "ready")

    def test_regression_blocks_and_creates_fix(self):
        _, _, (ui, _) = self.plan_with_tasks()
        oc.promote(self.root)
        self.dispatch_and_finish(ui)
        route = self.review(ui, fc="REGRESSION")
        t = self.task(ui)
        fix = t["blocked_by"][-1]
        self.assertIn("BLOCK MERGE", route)
        self.assertEqual(self.task(fix)["owner"], "coder")
        self.assertEqual(self.task(fix)["write_scope"], t["write_scope"])
        oc.set_fields(self.root, fix, {"status": "done"})
        oc.promote(self.root)
        self.assertEqual(self.task(ui)["status"], "in_review")

    def test_decision_conflict_asks_user(self):
        _, _, (ui, _) = self.plan_with_tasks()
        oc.promote(self.root)
        self.dispatch_and_finish(ui)
        self.assertIn("ASK USER", self.review(ui, fc="DECISION_CONFLICT"))
        self.assertTrue(self.task(ui)["needs_user"])

    def test_envelope_rejections(self):
        bad = [
            "I am done already.",                                        # prose
            "msg: result\ntask_id: TASK-0001\nstatus: ok\nsummary: x",  # status sai
            "msg: result\ntask_id: TASK-0001\nstatus: done\nsummary: x\nreasoning: y",
            "msg: result\ntask_id: TASK-0001\nstatus: done\nsummary: " + "a" * 201,
        ]
        for text in bad:
            with self.assertRaises(oc.OrcError, msg=text):
                oc.parse_envelope(text)

    def test_review_fail_needs_failure_class(self):
        _, _, (ui, _) = self.plan_with_tasks()
        oc.promote(self.root)
        self.dispatch_and_finish(ui)
        with self.assertRaises(oc.OrcError):
            self.review(ui, fc=None)

    def test_apply_rejects_wrong_phase(self):
        t = oc.new_task(self.root, title="x")["id"]
        with self.assertRaises(oc.OrcError):
            self.review(t, status="done")


class TestHandoffAndHooks(Base):
    def fill(self, sid, result="done"):
        path = oc.content_path(self.root, sid)
        fm, body = oc.split_frontmatter(path.read_text(encoding="utf-8"))
        fm["result"] = result
        oc.write_text(path, oc.join_frontmatter(fm, body.replace(oc.PLACEHOLDER, "None")))

    def test_handoff_requires_sections(self):
        sid = oc.handoff_open(self.root, "orchestrator", key="k1")
        errs = oc.handoff_errors(self.root, sid)
        self.assertTrue(any("result" in e for e in errs))
        self.assertTrue(any("not filled in" in e for e in errs))
        self.fill(sid)
        self.assertEqual(oc.handoff_errors(self.root, sid), [])
        self.assertEqual(oc.validate(self.root), [])

    def test_one_open_session_per_key(self):
        oc.handoff_open(self.root, "orchestrator", key="k1")
        with self.assertRaises(oc.OrcError):
            oc.handoff_open(self.root, "orchestrator", key="k1")

    def test_cli_validate_handoff(self):
        sid = oc.handoff_open(self.root, "orchestrator", key="k1")
        self.assertEqual(cli(self.root, "validate", "--handoff", "--key", "k1")[0], 1)
        self.assertEqual(cli(self.root, "validate", "--handoff")[0], 1)
        self.fill(sid)
        self.assertEqual(cli(self.root, "validate", "--handoff", "--key", "k1")[0], 0)
        code, out, _ = cli(self.root, "handoff", "check", "--key", "k1")
        self.assertEqual(code, 0)
        self.assertIn("is valid", out)

    def test_brief_contents_and_size(self):
        for i in range(40):
            oc.new_task(self.root, title=f"Job number {i} " + "x" * 60, summary="s" * 200)
        oc.new_decision(self.root, "Use SQLite WASM", status="accepted")
        sid = oc.handoff_open(self.root, "orchestrator", key="old")
        self.fill(sid)
        path = oc.content_path(self.root, sid)
        oc.write_text(path, path.read_text(encoding="utf-8").replace(
            "## Next steps\nNone", "## Next steps\nRun /run TASK-0002"))
        text = oc.brief_text(self.root, key="new-key")
        self.assertIn("Goal: Try orchestra", text)
        self.assertIn("DEC-0001", text)
        self.assertIn("Run /run TASK-0002", text)
        self.assertIn("orc handoff open --key new-key", text)
        self.assertLessEqual(len(text), oc.BRIEF_MAX_CHARS)

    def test_hooks_noop_without_orchestra(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("session_start", "stop_check"):
                code, out, err = hook(name, {"cwd": d, "session_id": "x"})
                self.assertEqual((code, out.strip()), (0, ""), (name, err))

    def test_session_start_hook_outputs_brief(self):
        code, out, err = hook("session_start", {"cwd": str(self.root / ".orchestra"),
                                                "session_id": "abc"})
        self.assertEqual(code, 0, err)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Orchestra brief", ctx)
        self.assertIn("--key abc", ctx)

    def test_stop_hook_only_blocks_open_session(self):
        payload = {"cwd": str(self.root), "session_id": "s1"}
        self.assertEqual(hook("stop_check", payload)[1].strip(), "")  # no S-ID opened yet
        sid = oc.handoff_open(self.root, "orchestrator", key="s1")
        out = hook("stop_check", payload)[1]
        self.assertEqual(json.loads(out)["decision"], "block")
        self.fill(sid)
        self.assertEqual(hook("stop_check", payload)[1].strip(), "")
        self.assertTrue(oc.load_yaml_file(oc.marker_path(self.root, "s1"))["closed"])

    def test_stop_hook_gives_up_after_limit(self):
        oc.handoff_open(self.root, "orchestrator", key="s2")
        payload = {"cwd": str(self.root), "session_id": "s2"}
        for _ in range(oc.MAX_STOP_BLOCKS):
            self.assertIn("block", hook("stop_check", payload)[1])
        self.assertEqual(hook("stop_check", payload)[1].strip(), "")

    def test_hooks_survive_garbage_stdin(self):
        for name in ("session_start", "stop_check"):
            r = subprocess.run([PY, str(KIT / "hooks" / name)], input="not json",
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)


class TestGateAndReport(Base):
    def write_plan(self, asks):
        t = oc.new_task(self.root, title="Request", owner="researcher")["id"]
        art = oc.new_artifact(self.root, "plan", t)["id"]
        path = oc.content_path(self.root, art)
        fm, _ = oc.split_frontmatter(path.read_text(encoding="utf-8"))
        body = PLAN_BODY + f"\n## Decisions for the user\n<!-- note -->\n{asks}\n"
        oc.write_text(path, oc.join_frontmatter(fm, body))
        return t, art

    def test_gate_proceeds_when_nothing_to_ask(self):
        _, art = self.write_plan("- None")
        self.assertEqual(oc.gate(self.root, art), [])
        code, out, _ = cli(self.root, "gate", art)
        self.assertEqual((code, out.strip()), (0, "PROCEED"))

    def test_gate_asks_on_questions_and_proposed_decisions(self):
        t, art = self.write_plan("- Change the order lifecycle?")
        oc.new_decision(self.root, "Add a new table", scope=t)
        reasons = oc.gate(self.root, art)
        self.assertTrue(any("lifecycle" in r for r in reasons))
        self.assertTrue(any("DEC-0001" in r for r in reasons))
        self.assertEqual(cli(self.root, "gate", art)[0], 3)

    def test_gate_asks_when_section_or_tasks_missing(self):
        t = oc.new_task(self.root, title="x", owner="researcher")["id"]
        art = oc.new_artifact(self.root, "plan", t)["id"]  # template: has AC-1 and the sample orc-tasks
        path = oc.content_path(self.root, art)
        fm, _ = oc.split_frontmatter(path.read_text(encoding="utf-8"))
        oc.write_text(path, oc.join_frontmatter(fm, "# empty\n"))
        reasons = oc.gate(self.root, art)
        self.assertTrue(any("missing the section" in r for r in reasons))
        self.assertTrue(any("orc-tasks" in r for r in reasons))
        self.assertTrue(any("AC-n" in r for r in reasons))

    def test_ask_requires_filled_understanding(self):
        t = oc.new_task(self.root, title="x", owner="researcher")["id"]
        art = oc.new_artifact(self.root, "research", t, summary="Understand B2")["id"]
        with self.assertRaises(oc.OrcError):
            oc.ask_text(self.root, art)  # still has _(fill in)_
        path = oc.content_path(self.root, art)
        fm, body = oc.split_frontmatter(path.read_text(encoding="utf-8"))
        body = body.replace(oc.PLACEHOLDER, "Q1: Limit to 20 MB? — Recommended: yes", 1)
        body = body.replace(oc.PLACEHOLDER, "None")
        oc.write_text(path, oc.join_frontmatter(fm, body))
        text = oc.ask_text(self.root, art)
        self.assertIn("## Questions for the user", text)
        self.assertIn("Q1: Limit to 20 MB?", text)
        self.assertNotIn("<!--", text)
        with self.assertRaises(oc.OrcError):
            oc.ask_text(self.root, oc.new_artifact(self.root, "plan", t)["id"])

    def test_gate_rejects_untouched_template(self):
        t = oc.new_task(self.root, title="x", owner="researcher")["id"]
        art = oc.new_artifact(self.root, "plan", t)["id"]
        reasons = oc.gate(self.root, art)
        self.assertTrue(any("template tasks" in r for r in reasons), reasons)
        self.assertTrue(any("template criteria" in r for r in reasons), reasons)

    def test_report_covers_tasks_evidence_and_decisions(self):
        t, art = self.write_plan("None")
        oc.set_fields(self.root, art, {"status": "accepted"})
        ui, doc = [e["id"] for e in oc.tasks_from_plan(self.root, art)]
        oc.promote(self.root)
        self.dispatch_and_finish(ui)
        self.review(ui, status="done")
        oc.new_decision(self.root, "CSV adds quotes", scope=art, status="accepted")
        text = oc.report(self.root, t)
        self.assertIn(f"Plan {art}", text)
        self.assertIn(f"| {ui}", text)
        self.assertIn("test:pass", text)
        self.assertIn("app/src/features/settings/Clear.tsx", text)
        self.assertIn("DEC-0001", text)
        self.assertIn(f"1 tasks not done: {doc}[pending]", text)
        self.assertEqual(cli(self.root, "report", t)[0], 0)


if __name__ == "__main__":
    unittest.main()
