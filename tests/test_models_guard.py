"""0.4.0: model per subagent, journal model, config checked up front, no false approval_missing in K,
opt-in enforcing guard rules, one overlapping K request at a time, plan ARTs listed in docs/INDEX.md."""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_workflow_k import KIT, Base, cli  # noqa: E402

import orc_core as oc  # noqa: E402
import orc_guard as og  # noqa: E402
import orc_k  # noqa: E402


def frontmatter(path: Path) -> dict:
    fm, _ = oc.split_frontmatter(path.read_text(encoding="utf-8"))
    return fm


class TestAgentModels(unittest.TestCase):
    def test_frontmatter_models(self):  # AC-1
        want = {"researcher": "opus", "reviewer": "opus", "coder": "sonnet", "context-builder": "sonnet"}
        for name, model in want.items():
            self.assertEqual(frontmatter(KIT / "agents" / f"{name}.md").get("model"), model, name)

    def test_commands_pass_models(self):
        for p in [*(KIT / "commands").glob("*.md"), KIT / "references" / "auto-legacy.md"]:
            text = p.read_text(encoding="utf-8")
            if "subagent" in text:
                self.assertIn("orc model <TASK-ID>", text, p.name)


class KBase(Base):
    def config(self, extra: str):
        with open(self.root / ".orchestra/config.yaml", "a", encoding="utf-8") as f:
            f.write(extra)

    def coder_line(self, out, tid):
        return next(l for l in out.splitlines() if l.startswith(f"READY: {tid}"))


class TestModelsInNext(KBase):
    def test_coder_sonnet_then_opus_after_send_back(self):  # AC-2
        R, UND, DEC, PLAN, C = self.to_planned()
        self.approve(R)
        line = self.coder_line(self.ok("next"), C)
        self.assertIn("agent orcgate:coder model=sonnet", line)
        self.assertIn("context: orcgate:context-builder model=sonnet", line)
        self.assertEqual(self.ok("model", C), "sonnet")
        oc.set_fields(self.root, C, {"attempts": 1})
        self.assertIn("model=opus", self.coder_line(self.ok("next"), C))
        self.assertEqual(self.ok("model", C), "opus")
        self.assertEqual(self.ok("model", C, "--role", "context-builder"), "sonnet")

    def test_review_and_researcher_models(self):  # AC-3
        R, UND, DEC, PLAN, C = self.to_planned()
        self.approve(R)
        self.implement(C)
        out = self.ok("next")
        self.assertRegex(out, rf"REVIEW: {C} .*agent orcgate:reviewer model=opus")
        self.assertEqual(self.ok("model", C, "--role", "reviewer"), "opus")
        PT = next(t["id"] for t in oc.load_registry(self.root, "task") if t["title"] == "Plan")
        self.assertEqual(self.ok("model", PT), "opus")
        self.assertEqual(self.ok("model", R), "inherit")  # orchestrator = the main session

    def test_config_override_and_errors(self):  # AC-4
        R, UND, DEC, PLAN, C = self.to_planned()
        self.config("models:\n  coder: opus\n")
        self.assertEqual(self.ok("model", C), "opus")
        self.config("  tester: opus\n  reviewer: gpt\n")
        code, out, _ = cli(self.root, "validate")
        self.assertEqual(code, 1)
        self.assertIn("models.tester", out)
        self.assertIn("models.reviewer: 'gpt'", out)
        self.no("model", C)

    def test_model_recorded_in_journal_and_report(self):  # AC-5, AC-6
        R, UND, DEC, PLAN, C = self.to_planned()
        self.approve(R)
        self.ok("next")
        self.no("advance", C, "--to", "dispatched", "--model", "gpt-5")
        self.ok("advance", C, "--to", "dispatched", "--model", "sonnet")
        rows = [json.loads(l) for l in oc.journal_path(self.root).read_text(encoding="utf-8").splitlines()]
        row = next(r for r in reversed(rows) if r.get("id") == C and r.get("to") == "dispatched")
        self.assertEqual(row["model"], "sonnet")
        self.assertIn("checkpoint", row)  # model and checkpoint both kept
        self.assertEqual(self.ok("validate"), "OK")  # hash chain intact
        PT = next(t["id"] for t in oc.load_registry(self.root, "task") if t["title"] == "Plan")
        report = self.ok("report", PT)
        self.assertIn("| Model |", report)
        self.assertRegex(report, rf"\| {C} .*\| sonnet \|")


class TestConfigChecks(KBase):
    def test_bad_verify_command_name_reported_early(self):  # T7
        self.config("    deps: {run: 'node check-deps.cjs'}\n")
        code, out, _ = cli(self.root, "validate")
        self.assertEqual(code, 1)
        self.assertIn("'deps' is not one of", out)
        self.assertIn("belongs under `lint`", out)
        self.assertIn("config: ERROR", oc.brief_text(self.root))

    def test_valid_config_has_no_brief_error(self):
        self.config("models:\n  coder: sonnet\nguard:\n  enforce: [edit_write]\n  always_writable: [HANDOFF.md]\n")
        self.assertEqual(self.ok("validate"), "OK")
        self.assertNotIn("config: ERROR", oc.brief_text(self.root))

    def test_bad_guard_section(self):
        self.config("guard:\n  enforce: [everything]\n")
        code, out, _ = cli(self.root, "validate")
        self.assertEqual(code, 1)
        self.assertIn("guard.enforce", out)


class TestNoFalseApprovalMissing(KBase):
    def test_k_child_dispatch_writes_no_shadow_line(self):  # T8
        R, UND, DEC, PLAN, C = self.to_planned()
        self.approve(R)
        self.ok("next")
        self.ok("advance", C, "--to", "dispatched")
        shadow = self.root / ".orchestra/runtime/shadow.jsonl"
        lines = shadow.read_text(encoding="utf-8") if shadow.exists() else ""
        self.assertNotIn("approval_missing", lines)


class TestEnforcingGuard(KBase):
    def payload(self, tool, **ti):
        return {"cwd": str(self.root), "session_id": "s", "tool_name": tool, "tool_input": ti}

    def run_guard(self, tool, **ti):
        return og.shadow(self.payload(tool, **ti))

    def test_shadow_by_default(self):
        R, *_ = self.to_planned()
        rec = self.run_guard("Edit", file_path=str(self.root / "app/x.ts"))
        self.assertNotIn("enforced", rec)

    def test_edit_blocked_while_request_open_without_coder(self):  # T9 (a)
        self.config("guard:\n  enforce: [edit_write, git_push]\n  always_writable: [NOTES.md]\n")
        R, UND, DEC, PLAN, C = self.to_planned()
        rec = self.run_guard("Edit", file_path=str(self.root / "app/x.ts"))
        self.assertIn("no coder task is running", rec["enforced"])
        self.assertIn(R, rec["enforced"])
        self.assertNotIn("enforced", self.run_guard("Write", file_path=str(self.root / "NOTES.md")))
        self.assertIn("enforced", self.run_guard("Write", file_path=str(self.root / ".orchestra/state/tasks.yaml")))

    def test_coder_limited_to_write_scope(self):  # T9 (b)
        self.config("guard:\n  enforce: [edit_write]\n")
        R, UND, DEC, PLAN, C = self.to_planned()
        self.approve(R)
        self.ok("next")
        self.ok("advance", C, "--to", "dispatched")
        self.assertNotIn("enforced", self.run_guard("Write", file_path=str(self.root / "app/button.ts")))
        rec = self.run_guard("Write", file_path=str(self.root / "src/other.ts"))
        self.assertIn("outside the write_scope", rec["enforced"])

    def test_no_open_request_allows_edits(self):
        self.config("guard:\n  enforce: [edit_write]\n")
        oc.write_active(self.root)
        self.assertNotIn("enforced", self.run_guard("Edit", file_path=str(self.root / "app/x.ts")))

    def test_git_push_blocked_only_when_enabled(self):
        self.assertNotIn("enforced", self.run_guard("Bash", command="git push origin main"))
        self.config("guard:\n  enforce: [git_push]\n")
        rec = self.run_guard("Bash", command="git -C . push origin main")
        self.assertIn("git push", rec["enforced"])
        self.assertNotIn("enforced", self.run_guard("Bash", command="git status && git log -1"))
        self.assertNotIn("enforced", self.run_guard("Edit", file_path=str(self.root / "app/x.ts")))

    def test_hook_exit_code_2(self):
        import subprocess
        self.config("guard:\n  enforce: [git_push]\n")
        r = subprocess.run([sys.executable, str(KIT / "hooks" / "guard")], capture_output=True, text=True,
                           input=json.dumps(self.payload("Bash", command="git push")))
        self.assertEqual(r.returncode, 2)
        self.assertIn("blocked", r.stderr)

    def test_shadow_false_positives_fixed(self):
        view = og.shell_view("sed -n 1,80p .orchestra/artifacts/ART-0001.md; git merge-base HEAD main")
        self.assertEqual(og.write_targets(view), [])
        self.assertEqual(og.decide_shell(self.root, "git merge-base HEAD main", {})[0], "allow")
        self.assertEqual(og.decide_shell(self.root, "git merge main", {})[0], "deny")
        self.assertEqual(og.decide_shell(self.root, "sed -i s/a/b/ .orchestra/state/tasks.yaml", {})[0], "deny")


class TestOneRequestAtATime(KBase):
    def second_request(self, scope):
        body = PLAN_TEMPLATE.replace("SCOPE", scope)
        R2 = self.ok("new", "request", "--title", "Second", "--summary", "s")
        self.ok("advance", R2, "--to", "TRIAGED")
        self.ok("advance", R2, "--to", "RESEARCHING")
        UT = self.ok("new", "task", "--title", "Understand 2", "--owner", "researcher", "--parent", R2)
        UND = self.ok("new", "artifact", "--type", "research", "--task", UT)
        self.fill(UND, UND_BODY_2)
        self.ok("advance", UT, "--to", "dispatched")
        self.ok("apply", "-", stdin=self.env(UT, outputs=[UND]))
        PT = self.ok("new", "task", "--title", "Plan 2", "--owner", "researcher", "--parent", R2)
        PLAN = self.ok("new", "artifact", "--type", "plan", "--task", PT)
        self.fill(PLAN, body)
        self.ok("advance", PT, "--to", "dispatched")
        self.ok("apply", "-", stdin=self.env(PT, outputs=[PLAN]))
        self.ok("advance", PLAN, "--to", "accepted")
        self.ok("new", "task", "--from", PLAN)
        self.ok("advance", R2, "--to", "PLANNED")
        out = self.ok("advance", R2, "--to", "APPROVAL_REQUIRED")
        line = next(l.strip() for l in out.splitlines() if l.strip().startswith("orc:approve"))
        self.say(line)
        self.ok("approve", *line.split()[1].split(","))
        return R2

    def test_overlapping_scope_refused(self):  # T10
        R, *_ = self.to_planned()
        self.approve(R)  # R: app/
        R2 = self.second_request("app/sub/")
        err = self.no("advance", R2, "--to", "IMPLEMENTING")
        self.assertIn("overlapping write_scope", err)
        self.assertIn(R, err)

    def test_disjoint_scope_allowed_with_warning(self):
        R, *_ = self.to_planned()
        self.approve(R)
        R2 = self.second_request("lib/")
        out = self.ok("advance", R2, "--to", "IMPLEMENTING")
        self.assertIn("warning", out)

    def test_scopes_overlap_helper(self):
        self.assertTrue(orc_k.scopes_overlap(["renderer/src/**/*.ts"], ["renderer/src/a.ts"]))
        self.assertTrue(orc_k.scopes_overlap(["docs/"], ["docs/x/y.md"]))
        self.assertFalse(orc_k.scopes_overlap(["electron/**"], ["backend/x.py"]))
        self.assertTrue(orc_k.scopes_overlap(["**/*.md"], ["backend/x.py"]))  # conservative

    def test_stale_marker_in_brief(self):
        import datetime
        R, *_ = self.to_planned()
        self.assertNotIn("stale", oc.brief_text(self.root))
        later = datetime.datetime.now() + datetime.timedelta(days=9)
        self.assertGreater(orc_k.stale_days(self.root, R, now=later), 7)


class TestPlansIndex(KBase):
    def test_docs_index_lists_accepted_plans(self):  # AC-7
        R, UND, DEC, PLAN, C = self.to_planned()
        (self.root / "docs").mkdir()
        (self.root / "docs/a.md").write_text(
            "---\nstatus: current\nfeature: x\nupdated: 2026-11-24\nsummary: a\nread_when: x\nsupersedes: null\n---\n# A\n",
            encoding="utf-8")
        self.ok("docs", "index")
        first = (self.root / "docs/INDEX.md").read_text(encoding="utf-8")
        self.assertIn("### Plans (orcgate)", first)
        self.assertIn(f"[{PLAN}](../.orchestra/artifacts/{PLAN}.md)", first)
        self.assertNotIn(f"[{UND}]", first)  # research ART not accepted yet
        self.ok("docs", "index")
        self.assertEqual((self.root / "docs/INDEX.md").read_text(encoding="utf-8"), first)  # deterministic
        self.ok("docs", "check")

    def test_superseded_plan_hidden(self):
        R, UND, DEC, PLAN, C = self.to_planned()
        PT = next(t["id"] for t in oc.load_registry(self.root, "task") if t["title"] == "Plan")
        P2 = self.ok("new", "artifact", "--type", "plan", "--task", PT, "--supersedes", PLAN)
        self.fill(P2, "x")
        oc.set_fields(self.root, P2, {"status": "accepted"})
        lines = "\n".join(oc.plans_index_lines(self.root))
        self.assertIn(f"[{P2}]", lines)
        self.assertNotIn(f"[{PLAN}]", lines)


UND_BODY_2 = """
## My understanding of the request
Second.

## Preliminary design
One.

## Unclear / missing information
None

## Questions for the user
Q1: ok? — Recommended: yes
"""
PLAN_TEMPLATE = """
## Acceptance criteria
- AC-1: done

## Decisions for the user
None

## Task
```yaml orc-tasks
- key: a
  title: Second change
  owner: coder
  acceptance: [AC-1]
  write_scope: ["SCOPE"]
```
"""

if __name__ == "__main__":
    unittest.main()
