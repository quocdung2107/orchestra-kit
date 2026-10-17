"""UserPromptSubmit hook + orc approve, PreToolUse guard (shadow), SAFE_MODE."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
KIT = REPO / "plugins" / "orchestra"  # plugin root (cli/, hooks/, templates/…)
sys.path.insert(0, str(KIT / "cli"))
import orc_approval as oa  # noqa: E402
import orc_core as oc  # noqa: E402
import orc_guard as og  # noqa: E402

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
  write_scope: ["app/src/"]
```
"""


def hook(name, payload, env=None):
    r = subprocess.run([PY, str(KIT / "hooks" / name)], input=json.dumps(payload).encode("utf-8"),
                       capture_output=True, env=env)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


def cli(root, *args, env=None):
    r = subprocess.run([PY, str(KIT / "cli" / "orc"), "-C", str(root), *args], capture_output=True, env=env)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = r = Path(self.tmp.name)
        oc.cmd_init(r, "p", "g")
        pt = oc.new_task(r, title="Plan", owner="researcher")["id"]
        self.plan = oc.new_artifact(r, "plan", pt)["id"]
        path = oc.content_path(r, self.plan)
        oc.write_text(path, oc.join_frontmatter(oc.split_frontmatter(oc.read_text(path))[0], PLAN))
        oc.advance(r, self.plan, "accepted")
        self.t = oc.tasks_from_plan(r, self.plan)[0]["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def say(self, text):
        return hook("prompt_log", {"session_id": "s1", "cwd": str(self.root), "prompt": text})


class TestPromptLog(Base):
    def test_logs_only_orc_lines_with_chain(self):
        self.say("My bank account is 123\norc:approve TASK-0002 ABC123\ndone")
        self.say("an ordinary sentence")
        rows = oa.prompt_lines(self.root)
        self.assertEqual(rows[0]["orc"], ["orc:approve TASK-0002 ABC123"])
        self.assertEqual(rows[1]["orc"], [])
        text = og.prompts_path(self.root).read_text(encoding="utf-8")
        self.assertNotIn("bank account", text)
        self.assertNotIn("ordinary sentence", text)

    def test_noop_without_orchestra(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, _ = hook("prompt_log", {"cwd": d, "prompt": "orc:approve X"})
            self.assertEqual(code, 0)
            self.assertFalse((Path(d) / ".orchestra").exists())


class TestApprove(Base):
    def nonce(self):
        out = oa.request(self.root, self.t)
        return out.split(f"orc:approve {self.t} ")[1].split()[0]

    def test_happy_path_and_single_use(self):
        n = self.nonce()
        self.say(f"Approved\norc:approve {self.t} {n}")
        req = oa.approve(self.root, self.t)
        self.assertEqual(req["status"], "used")
        self.assertEqual(oa.check(self.root, self.t)[0], True)
        with self.assertRaises(oc.OrcError):  # nonce already used
            oa.approve(self.root, self.t)

    def test_rejects_without_prompt_wrong_nonce_or_before_request(self):
        self.say(f"orc:approve {self.t} 000000")
        n = self.nonce()
        with self.assertRaises(oc.OrcError):  # no user prompt with the right nonce yet
            oa.approve(self.root, self.t)
        self.say(f"orc:approve {self.t} FFFFFF")
        with self.assertRaises(oc.OrcError):  # nonce sai
            oa.approve(self.root, self.t)
        self.say(f"ok approved")  # a generic "ok" is not enough
        with self.assertRaises(oc.OrcError):
            oa.approve(self.root, self.t)
        self.assertEqual(len(n), 6)

    def test_prompt_before_request_does_not_count(self):
        # the user typed before orc issued the nonce: it cannot match the new nonce, and ts is before the request
        oa.request(self.root, self.t)
        items = oa.load(self.root)
        n = items[-1]["nonce"]
        self.say(f"orc:approve {self.t} {n}")
        items[-1]["requested_at"] = "9999-01-01T00:00:00.000000"
        oa.save(self.root, items)
        with self.assertRaises(oc.OrcError):
            oa.approve(self.root, self.t)

    def test_binding_change_invalidates(self):
        for change in ("plan", "scope", "level", "risk"):
            with self.subTest(change):
                n = self.nonce()
                if change == "plan":
                    p = oc.content_path(self.root, self.plan)
                    oc.write_text(p, oc.read_text(p).replace("there is a button", "there is a red button"))
                elif change == "scope":
                    oc.set_fields(self.root, self.t, {"write_scope": ["app/"]})
                elif change == "level":
                    items = oc.load_registry(self.root, "task")
                    for it in items:
                        if it["id"] == self.t:
                            it["level"] = 3
                    oc.save_registry(self.root, "task", items)
                else:
                    items = oc.load_registry(self.root, "task")
                    for it in items:
                        if it["id"] == self.t:
                            it["risk_flags"] = ["migration"]
                    oc.save_registry(self.root, "task", items)
                self.say(f"orc:approve {self.t} {n}")
                with self.assertRaises(oc.OrcError):
                    oa.approve(self.root, self.t)
        # approval used, then the binding changes ⇒ check reports it expired
        n = self.nonce()
        self.say(f"orc:approve {self.t} {n}")
        oa.approve(self.root, self.t)
        oc.set_fields(self.root, self.t, {"write_scope": ["app/src/x.ts"]})
        ok, why = oa.check(self.root, self.t)
        self.assertFalse(ok)
        self.assertIn("write_scope_hash", why)

    def test_tampered_prompt_log_refused(self):
        n = self.nonce()
        self.say("sentence 1")
        self.say(f"orc:approve {self.t} {n}")
        p = og.prompts_path(self.root)
        lines = p.read_text(encoding="utf-8").splitlines()
        p.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")  # delete the first line
        with self.assertRaises(oc.OrcError):
            oa.approve(self.root, self.t)

    def test_cli_flow_and_dispatch_shadow(self):
        code, out, err = cli(self.root, "approval", "request", self.t)
        self.assertEqual(code, 0, err)
        self.assertIn(f"orc:approve {self.t} ", out)
        oc.advance(self.root, self.t, "dispatched")
        shadow = (self.root / ".orchestra/runtime/shadow.jsonl").read_text(encoding="utf-8")
        self.assertIn("approval_missing", shadow)
        self.assertEqual(cli(self.root, "approval", "check", self.t)[0], 3)


class TestGuardDecide(Base):
    def act(self, statuses):
        tasks = []
        for tid, status, owner, scope, arts in statuses:
            tasks.append({"id": tid, "status": status, "owner": owner, "write_scope": scope, "artifacts": arts})
        return {"tasks": tasks, "writable": [a for t in tasks for a in t["artifacts"]]}

    def d(self, active, tool, ti, **kw):
        return og.decide(self.root, active, tool, ti, safe=False, **kw)[0]

    def test_normal_mode_allows_but_deny_always(self):
        a = self.act([])
        self.assertEqual(self.d(a, "Edit", {"file_path": str(self.root / "app/x.ts")}), "allow")
        self.assertEqual(self.d(a, "Bash", {"command": "npm install left-pad"}), "allow")
        for cmd in ("git push origin main", "git -C app push", "git reset --hard HEAD~1", "rm -rf app",
                    "supabase db push", "npm publish", "psql -h db.example.com", "echo x > .orchestra/state/tasks.yaml",
                    "Remove-Item -Recurse app", "git merge x", "git push --force-with-lease"):
            self.assertEqual(self.d(a, "Bash", {"command": cmd}), "deny", cmd)
        self.assertEqual(self.d(a, "Bash", {"command": "psql -h localhost"}), "allow")
        self.assertEqual(self.d(a, "Bash", {"command": "orc show TASK-0001 && orc next"}), "allow")
        for f in (".orchestra/state/tasks.yaml", ".orchestra/runtime/prompts.jsonl",
                  ".orchestra/evidence/EVD-0001.md", ".claude/settings.json"):
            self.assertEqual(self.d(a, "Write", {"file_path": f}), "deny", f)
        self.assertEqual(self.d(a, "Write", {"file_path": str(Path.home() / ".claude/settings.json")}), "deny")

    def test_implementing(self):
        a = self.act([("TASK-0002", "dispatched", "coder", ["app/src/"], [])])
        self.assertEqual(self.d(a, "Edit", {"file_path": str(self.root / "app/src/a.ts")}), "allow")
        self.assertEqual(self.d(a, "Edit", {"file_path": "app/package.json"}), "deny")
        self.assertEqual(self.d(a, "Write", {"file_path": "C:/elsewhere/x.txt"}), "deny")
        self.assertEqual(self.d(a, "Bash", {"command": "cd app && npm run test 2>&1 | tail -5"}), "allow")
        self.assertEqual(self.d(a, "Bash", {"command": "git add -A && git commit -m x"}), "allow")
        # Bash is not a sandbox — writes through Bash outside deny_always areas are checked afterwards by orc verify
        self.assertEqual(self.d(a, "Bash", {"command": "npm install left-pad"}), "allow")
        self.assertEqual(self.d(a, "Bash", {"command": "python -c \"open('x','w')\""}), "allow")
        self.assertEqual(self.d(a, "Bash", {"command": "cat a > b.txt"}), "allow")
        self.assertEqual(self.d(a, "Bash", {"command": "sed -i s/a/b/ x"}), "allow")
        self.assertEqual(self.d(a, "Bash", {"command": "sed -i s/a/b/ .orchestra/state/tasks.yaml"}), "deny")
        self.assertEqual(self.d(a, "WebFetch", {"url": "https://x"}), "deny")
        self.assertEqual(self.d(a, "Read", {"file_path": "anything"}), "allow")

    def test_research_and_verify(self):
        art = f".orchestra/artifacts/{self.plan}.md"
        a = self.act([("TASK-0001", "dispatched", "researcher", [], [art])])
        self.assertEqual(self.d(a, "Edit", {"file_path": art}), "allow")
        self.assertEqual(self.d(a, "Edit", {"file_path": "app/src/a.ts"}), "deny")
        self.assertEqual(self.d(a, "WebSearch", {"query": "x"}), "deny")
        self.assertEqual(self.d(a, "WebSearch", {"query": "x"}, agent_type="researcher-external"), "allow")
        # plugin mode: agent_type carries a namespace
        self.assertEqual(self.d(a, "WebSearch", {"query": "x"}, agent_type="orchestra:researcher-external"),
                         "allow")
        self.assertEqual(self.d(a, "WebSearch", {"query": "x"}, agent_type="other:researcher-external"), "deny")
        self.assertEqual(self.d(a, "WebSearch", {"query": "x"}, agent_type="orchestra:researcher"), "deny")
        self.assertEqual(self.d(a, "Bash", {"command": "git log --oneline -5"}), "allow")
        self.assertEqual(self.d(a, "Bash", {"command": "git commit -m x"}), "allow")  # checked afterwards
        v = self.act([("TASK-0002", "in_review", "coder", ["app/src/"], [".orchestra/artifacts/ART-0009.md"])])
        self.assertEqual(self.d(v, "Edit", {"file_path": "app/src/a.ts"}), "deny")
        self.assertEqual(self.d(v, "Edit", {"file_path": ".orchestra/artifacts/ART-0009.md"}), "allow")
        self.assertEqual(self.d(v, "Bash", {"command": "npm run build"}), "allow")

    def test_missing_active_json_fails_closed(self):
        with self.assertRaises(RuntimeError):
            og.decide(self.root, None, "Edit", {"file_path": "x"}, safe=False)


class TestGuardHookShadow(Base):
    def test_shadow_never_blocks_and_logs(self):
        oc.advance(self.root, self.t, "dispatched")  # writes active.json
        payload = {"session_id": "s1", "cwd": str(self.root), "tool_name": "Write", "agent_type": "coder",
                   "tool_input": {"file_path": str(self.root / "app/package.json"), "content": "{}"}}
        code, out, _ = hook("guard", payload)
        self.assertEqual((code, out), (0, ""))
        rec = json.loads((self.root / ".orchestra/runtime/guard-shadow.jsonl").read_text(encoding="utf-8")
                         .splitlines()[-1])
        self.assertEqual((rec["decision"], rec["mode"], rec["agent_type"]), ("deny", "implement", "coder"))
        self.assertIn("ms", rec)

    def test_shadow_on_v01_project_rebuilds_active_json(self):
        # Projects still on a pre-0.2 orc: no active.json; the guard rebuilds it from the registry without changing the registry
        items = oc.load_registry(self.root, "task")
        for it in items:
            if it["id"] == self.t:
                it["status"] = "dispatched"
        oc.save_registry(self.root, "task", items)
        (self.root / ".orchestra/runtime/active.json").unlink()
        before = oc.registry_path(self.root, "task").read_text(encoding="utf-8")
        hook("guard", {"cwd": str(self.root), "tool_name": "Write",
                       "tool_input": {"file_path": str(self.root / "app/package.json")}})
        rec = json.loads((self.root / ".orchestra/runtime/guard-shadow.jsonl").read_text(encoding="utf-8")
                         .splitlines()[-1])
        self.assertEqual((rec["mode"], rec["decision"]), ("implement", "deny"))
        self.assertEqual(oc.registry_path(self.root, "task").read_text(encoding="utf-8"), before)

    def test_hook_error_is_recorded(self):
        (self.root / ".orchestra/runtime/prompts.jsonl").mkdir(parents=True)  # writing into a directory ⇒ error
        code, _, err = hook("prompt_log", {"cwd": str(self.root), "prompt": "x"})
        self.assertEqual(code, 0)
        rows = (self.root / ".orchestra/runtime/hook-errors.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(rows[-1])["hook"], "prompt_log")

    def test_shadow_logs_error_as_would_deny(self):
        (self.root / ".orchestra/runtime/active.json").write_text("{broken", encoding="utf-8")
        code, _, _ = hook("guard", {"cwd": str(self.root), "tool_name": "Edit", "tool_input": {"file_path": "x"}})
        self.assertEqual(code, 0)
        rec = json.loads((self.root / ".orchestra/runtime/guard-shadow.jsonl").read_text(encoding="utf-8")
                         .splitlines()[-1])
        self.assertEqual((rec["decision"], rec["mode"]), ("deny", "error"))

    def test_noop_outside_orchestra(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = hook("guard", {"cwd": d, "tool_name": "Bash", "tool_input": {"command": "git push"}})
            self.assertEqual((code, out), (0, ""))


class TestSafeMode(Base):
    def env(self):
        return {**os.environ, "ORC_SAFE_MODE": "1"}

    def test_guard_safe_mode_only_kit_and_config(self):
        d = lambda tool, ti: og.decide(self.root, None, tool, ti, safe=True)[0]  # noqa: E731 - state not read
        self.assertEqual(d("Edit", {"file_path": str(KIT / "cli/orc_core.py")}), "allow")
        self.assertEqual(d("Edit", {"file_path": str(self.root / ".orchestra/config.yaml")}), "allow")
        self.assertEqual(d("Edit", {"file_path": str(self.root / "app/src/a.ts")}), "deny")
        self.assertEqual(d("Edit", {"file_path": str(self.root / ".orchestra/state/tasks.yaml")}), "deny")
        self.assertEqual(d("Bash", {"command": f"cd {KIT.as_posix()} && python -m unittest"}), "allow")
        self.assertEqual(d("Bash", {"command": "orc validate"}), "allow")
        # the guard does not parse orc commands; in SAFE_MODE `orc` itself refuses (test_orc_refuses_workflow_mutation)
        self.assertEqual(d("Bash", {"command": "orc advance TASK-0002 --to dispatched"}), "allow")
        # Bash outside the high-confidence deny rules is not blocked even in SAFE_MODE (checked afterwards with git)
        self.assertEqual(d("Bash", {"command": "npm run build"}), "allow")
        self.assertEqual(d("Bash", {"command": f"cd {KIT.as_posix()} && git push"}), "deny")

    def test_orc_refuses_workflow_mutation(self):
        env = self.env()
        for args in (("advance", self.t, "--to", "dispatched"), ("new", "task", "--title", "x"),
                     ("set", self.t, "summary=x"), ("next",), ("approve", self.t),
                     ("approval", "request", self.t)):
            code, _, err = cli(self.root, *args, env=env)
            self.assertEqual(code, 1, args)
            self.assertIn("ORC_SAFE_MODE", err)
        self.assertEqual(cli(self.root, "validate", env=env)[0], 0)
        code, out, _ = cli(self.root, "brief", env=env)
        self.assertEqual(code, 0)
        self.assertIn("ORC_SAFE_MODE is on", out)
        self.assertEqual(cli(self.root, "approval", "check", self.t, env=env)[0], 3)

    def test_safe_mode_guard_works_with_broken_state(self):
        (self.root / ".orchestra/runtime").mkdir(parents=True, exist_ok=True)
        (self.root / ".orchestra/runtime/active.json").write_text("{broken", encoding="utf-8")
        code, _, _ = hook("guard", {"cwd": str(self.root), "tool_name": "Edit",
                                    "tool_input": {"file_path": str(KIT / "cli/orc_guard.py")}}, env=self.env())
        self.assertEqual(code, 0)
        rec = json.loads((self.root / ".orchestra/runtime/guard-shadow.jsonl").read_text(encoding="utf-8")
                         .splitlines()[-1])
        self.assertEqual((rec["decision"], rec["mode"]), ("allow", "safe"))


class TestLatency(Base):
    def test_guard_hook_under_budget_on_this_machine(self):
        oc.advance(self.root, self.t, "dispatched")
        payload = json.dumps({"cwd": str(self.root), "tool_name": "Bash",
                              "tool_input": {"command": "npm run test"}}).encode("utf-8")
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            subprocess.run([PY, str(KIT / "hooks" / "guard")], input=payload, capture_output=True)
            times.append((time.perf_counter() - t0) * 1000)
        self.assertLess(sorted(times)[2], 1000, times)  # loose threshold for CI


if __name__ == "__main__":
    unittest.main()
