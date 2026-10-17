"""Guard Bash rules (high confidence), the LLM only proposes DECs, journal `rejected` lines, gitignore-style path
matching, ENVIRONMENT_FAILURE, Agent/SubagentHandback tools."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KIT = REPO / "plugins" / "orchestra"  # plugin root (cli/, hooks/, templates/…)
sys.path.insert(0, str(KIT / "cli"))
import orc_approval as oa  # noqa: E402
import orc_core as oc  # noqa: E402
import orc_guard as og  # noqa: E402
import orc_verify as ov  # noqa: E402

PY = sys.executable


def cli(root, *args, stdin=None, env=None):
    r = subprocess.run([PY, str(KIT / "cli" / "orc"), "-C", str(root), *args], capture_output=True, env=env,
                       input=stdin.encode("utf-8") if stdin else None)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def say(root, text):
    og.log_prompt({"cwd": str(root), "session_id": "user", "prompt": text})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        oc.cmd_init(self.root, "p", "g")

    def tearDown(self):
        self.tmp.cleanup()

    def journal(self):
        p = oc.journal_path(self.root)
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


# ---------------------------------------------------------------- F3

class TestPathMatching(unittest.TestCase):
    CASES = [
        ("app/src/a.ts", ["app/src/"], True),
        ("app/src/x/y.ts", ["app/src/"], True),
        ("app/srcx/a.ts", ["app/src/"], False),
        ("app/src/a.ts", ["app/src"], True),            # no glob: the file or a directory of that name
        ("app/src", ["app/src/"], True),
        ("docs/a.md", ["docs/*.md"], True),
        ("docs/x/a.md", ["docs/*.md"], False),          # `*` does not cross `/`
        ("app/scripts/seed-items.ts", ["app/scripts/seed-items.*"], True),
        ("app/scripts/seed-data.x/y/z.ts", ["app/scripts/seed-data.*"], True),  # a matching directory ⇒ its children
        ("supabase/migrations/2026_items_a.sql", ["supabase/migrations/2026*_items_*.sql"], True),
        ("supabase/migrations/sub/2026_items_a.sql", ["supabase/migrations/2026*_items_*.sql"], False),
        ("app/tests/a.test.ts", ["app/tests/**"], True),
        ("app/tests/x/a.test.ts", ["app/tests/**"], True),
        ("app/package.json", ["**/package.json"], True),
        ("package.json", ["**/package.json"], True),     # `**/` includes the root
        ("a/b/c/package.json", ["**/package.json"], True),
        ("x/node_modules/", ["**/node_modules/"], True),
        ("README.md", ["*.md"], True),
        ("docs/README.md", ["*.md"], False),            # anchored at the project root (unlike gitignore)
        ("./app/a.ts", ["app/"], True),
        ("app\\src\\a.ts", ["app/src/"], True),
        ("app/a.ts", [], False),
    ]

    def test_table(self):
        for path, pats, want in self.CASES:
            with self.subTest(path=path, pats=pats):
                self.assertEqual(og.match_path(path, pats), want)

    def test_apply_uses_new_semantics(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            oc.cmd_init(r, "p", "g")
            t = oc.new_task(r, title="x", owner="coder", write_scope=["docs/*.md"])["id"]
            oc.advance(r, t, "dispatched")
            with self.assertRaises(oc.OrcError):
                oc.apply_envelope(r, oc.parse_envelope(
                    f"msg: result\ntask_id: {t}\nstatus: done\noutputs: [docs/sub/a.md]\nsummary: x\n"))


# ---------------------------------------------------------------- Bash rules + Agent/SubagentHandback

class TestGuardBashRules(Base):
    def active(self, writable=(), protected=()):
        return {"tasks": [{"id": "TASK-0001", "status": "dispatched", "owner": "coder",
                           "write_scope": ["app/src/"], "artifacts": []}],
                "writable": list(writable), "protected": list(protected)}

    def d(self, tool, ti, active=None, **kw):
        return og.decide(self.root, active or self.active(), tool, ti, safe=False, **kw)[0]

    def test_phase3_false_positives_now_allowed(self):
        allow = [
            "cat > supabase/migrations/x.sql <<'EOF'\n-- run supabase db push after approval\nselect 1;\nEOF",
            "echo \"$(cat <<'YAML'\nmsg: result\nsummary: \"a; b | c && d\"\nYAML\n)\" | orc apply -",
            "cd my-app && grep -n \"updateItem\\|deleteItem\" src/a.ts | head",
            "cd my-app && (npm run build 2>&1 | tail -8; npm run test 2>&1 | tail -15)",
            "sed -n 52p src/shared/store/repo.ts | cut -c120-230",
            "git check-ignore -v app/src/a.ts",
            "orc new decision --summary \"User decided: Q1; Q2 | Q3\"",
            "python - <<'EOF'\nopen('app/src/a.ts','w').write('x > y; rm -rf z')\nEOF",
            "npm run lint 2>&1 | grep -E \"error|Found\"",
            "git commit -m \"no push --force; delete .orchestra/state\"",
        ]
        for cmd in allow:
            with self.subTest(cmd=cmd[:50]):
                self.assertEqual(self.d("Bash", {"command": cmd}), "allow")

    def test_high_confidence_denies(self):
        deny = ["git push", "git -C app push origin main", "git push --force-with-lease", "git merge feature",
                "git rebase main", "git reset --hard HEAD~1", "git -C app reset HEAD --hard", "git clean -fd",
                "git branch -D x", "supabase db push", "supabase db reset", "supabase functions deploy f",
                "supabase secrets set A=1", "npm publish", "firebase deploy", "vercel --prod",
                "gh release create v1", "rm -rf app", "rm -r x", "Remove-Item -Recurse app", "rmdir /s x",
                "find . -name '*.tmp' -delete", "psql -h db.example.com -c 'select 1'",
                "echo x > .orchestra/state/tasks.yaml", "echo x >> .orchestra/runtime/prompts.jsonl",
                "sed -i s/a/b/ .orchestra/config.yaml", "cp hook .git/hooks/pre-commit",
                "cp x app/.git/config", "echo x | tee .claude/settings.json",
                f"echo x > {(Path.home() / '.claude' / 'settings.json').as_posix()}", "echo x > ~/.claude/settings.json",
                "cat app/.env.local", "cp .env.example .env", "echo k > keys/server.pem",
                "mv x .orchestra/decisions/DEC-0001.md", "Set-Content -Path .orchestra/state/tasks.yaml -Value x"]
        for cmd in deny:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.d("Bash", {"command": cmd}), "deny")
        self.assertEqual(self.d("Bash", {"command": "psql -h localhost -c 'select 1'"}), "allow")
        self.assertEqual(self.d("Bash", {"command": "cat .orchestra/state/tasks.yaml"}), "allow")  # readable
        self.assertEqual(self.d("Bash", {"command": "cat .env.example"}), "allow")

    def test_own_artifact_writable_via_bash_and_edit(self):
        art = ".orchestra/artifacts/ART-0033.md"
        a = self.active(writable=[art])
        self.assertEqual(self.d("Bash", {"command": f"cat >> {art} <<'EOF'\nPASS\nEOF"}, a), "allow")
        self.assertEqual(self.d("Edit", {"file_path": art}, a), "allow")
        self.assertEqual(self.d("Edit", {"file_path": ".orchestra/artifacts/ART-0099.md"}, a), "deny")
        self.assertEqual(self.d("Bash", {"command": "cat >> .orchestra/artifacts/ART-0099.md"}, a), "deny")

    def test_edit_write_deny_always_and_scope(self):
        for f in (".orchestra/config.yaml", ".orchestra/state/tasks.yaml", ".orchestra/knowledge/K.md", ".git/config",
                  "app/.git/HEAD", ".claude/settings.json", str(Path.home() / ".claude" / "agents" / "x.md"),
                  "app/.env.local", "certs/a.key"):
            with self.subTest(f=f):
                self.assertEqual(self.d("Write", {"file_path": f}), "deny")
        self.assertEqual(self.d("Edit", {"file_path": "app/src/a.ts"}), "allow")
        self.assertEqual(self.d("Edit", {"file_path": "app/package.json"}), "deny")       # outside write_scope
        self.assertEqual(self.d("Write", {"file_path": str(Path(tempfile.gettempdir()) / "ed.py")}), "allow")
        a = self.active(protected=["config/prod.yaml"])
        self.assertEqual(self.d("Edit", {"file_path": "config/prod.yaml"}, a), "deny")

    def test_read_secrets(self):
        self.assertEqual(self.d("Read", {"file_path": str(self.root / "app/.env.local")}), "deny")
        self.assertEqual(self.d("Read", {"file_path": "app/.env.example"}), "allow")
        self.assertEqual(self.d("Grep", {"pattern": "x", "path": ".env"}), "deny")
        self.assertEqual(self.d("Read", {"file_path": "app/src/a.ts"}), "allow")

    def test_agent_tools_in_matrix(self):
        for tool in ("Agent", "SubagentHandback", "Task", "SendMessage"):
            with self.subTest(tool=tool):
                dec, reason, _ = og.decide(self.root, self.active(), tool, {"subagent_type": "coder"}, safe=False)
                self.assertEqual((dec, reason), ("allow", "tool does not write files"))
        dec, reason, _ = og.decide(self.root, self.active(), "NewFancyTool", {}, safe=False)
        self.assertEqual(dec, "allow")
        self.assertIn("unclassified tool", reason)

    def test_active_json_writable(self):
        t = oc.new_task(self.root, title="r", owner="researcher")["id"]
        art = oc.new_artifact(self.root, "research", t)["id"]
        sid = oc.handoff_open(self.root, "orchestrator")
        oc.write_active(self.root)
        act = og.load_active(self.root)
        self.assertIn(f".orchestra/artifacts/{art}.md", act["writable"])
        self.assertIn(f".orchestra/sessions/{sid}.md", act["writable"])


# ---------------------------------------------------------------- decision authority

class TestDecisionAuthority(Base):
    def test_cli_paths_cannot_accept(self):
        code, _, err = cli(self.root, "new", "decision", "--summary", "x", "--status", "accepted")
        self.assertEqual(code, 1)
        self.assertIn("the LLM only proposes", err)
        self.assertEqual(cli(self.root, "new", "decision", "--summary", "x", "--status", "superseded")[0], 1)
        d = cli(self.root, "new", "decision", "--summary", "a proposal")[1].strip()
        self.assertEqual(oc.find_entry(self.root, d)[2]["status"], "proposed")
        self.assertEqual(cli(self.root, "advance", d, "--to", "accepted")[0], 1)
        self.assertEqual(cli(self.root, "set", d, "status=accepted")[0], 1)
        t = oc.new_task(self.root, title="r", owner="researcher")["id"]
        und = oc.new_artifact(self.root, "research", t)["id"]
        self.assertEqual(cli(self.root, "advance", und, "--to", "accepted")[0], 1)
        oc.advance(self.root, t, "dispatched")
        oc.apply_envelope(self.root, oc.parse_envelope(
            f"msg: result\ntask_id: {t}\nstatus: done\nproposed_decisions: [{{summary: s, supersedes: {d}}}]\n"
            "summary: x\n"))
        self.assertTrue(all(x["status"] == "proposed" for x in oc.load_registry(self.root, "decision")))

    def test_accepted_dec_content_locked(self):
        d = oc.new_decision(self.root, "old", status="accepted")["id"]  # old data / internal API
        self.assertEqual(cli(self.root, "set", d, "summary=sneaky change")[0], 1)
        self.assertEqual(cli(self.root, "set", d, "scope=other")[0], 1)

    def test_approval_flow_multi_ids(self):
        old = oc.new_decision(self.root, "old", status="accepted")["id"]
        t = oc.new_task(self.root, title="r", owner="researcher")["id"]
        und = oc.new_artifact(self.root, "research", t)["id"]
        d1 = oc.new_decision(self.root, "Q1: A", scope=und)["id"]
        d2 = oc.new_decision(self.root, "Q2: B", scope=und, supersedes=old)["id"]
        code, out, err = cli(self.root, "approval", "request", und, d1, d2)
        self.assertEqual(code, 0, err)
        line = next(l.strip() for l in out.splitlines() if l.strip().startswith("orc:approve"))
        self.assertTrue(line.startswith(f"orc:approve {und},{d1},{d2} "))
        self.assertEqual(cli(self.root, "approve", und, d1, d2)[0], 1)   # no prompt from the user yet
        say(self.root, f"Agreed\n{line}")
        code, out, err = cli(self.root, "approve", und, d1, d2)
        self.assertEqual(code, 0, err)
        st = {e["id"]: e["status"] for e in oc.load_registry(self.root, "decision")}
        self.assertEqual((st[d1], st[d2], st[old]), ("accepted", "accepted", "superseded"))
        self.assertEqual(oc.find_entry(self.root, und)[2]["status"], "accepted")
        self.assertEqual(oc.validate(self.root), [])

    def test_changed_dec_after_request_refused(self):
        d = oc.new_decision(self.root, "Q1: A")["id"]
        out = oa.request(self.root, [d])
        oc.set_fields(self.root, d, {"summary": "Q1: B (edited after the approval request)"})
        say(self.root, next(l.strip() for l in out.splitlines() if "orc:approve" in l))
        with self.assertRaises(oc.OrcError):
            oa.approve(self.root, [d])
        self.assertEqual(oc.find_entry(self.root, d)[2]["status"], "proposed")


# ---------------------------------------------------------------- F2

class TestJournalRejected(Base):
    def test_rejections_logged_semantics_unchanged(self):
        t = cli(self.root, "new", "task", "--title", "x")[1].strip()
        before = oc.journal_path(self.root).read_text(encoding="utf-8")
        code, out, err = cli(self.root, "set", t, "status=done")
        self.assertEqual(code, 1)
        self.assertIn("orc set", err)
        row = self.journal()[-1]
        self.assertEqual((row["event"], row["task"], row["operation"], row["actor"]),
                         ("rejected", t, "set", "orc set"))
        self.assertIn("orc advance", row["reason"])
        self.assertEqual(len(row["reason_hash"]), 16)
        self.assertTrue(oc.journal_path(self.root).read_text(encoding="utf-8").startswith(before))  # old journal kept
        code, _, _ = cli(self.root, "apply", "-", stdin=f"msg: result\ntask_id: {t}\nstatus: done\nsummary: x\n")
        self.assertEqual(code, 1)  # task not dispatched
        self.assertEqual((self.journal()[-1]["task"], self.journal()[-1]["operation"]), (t, "apply"))
        self.assertEqual(cli(self.root, "new", "decision", "--summary", "x", "--status", "accepted")[0], 1)
        self.assertEqual(self.journal()[-1]["operation"], "new decision")
        n = len(self.journal())
        self.assertEqual(cli(self.root, "show", t)[0], 0)
        self.assertEqual(cli(self.root, "show", "TASK-0099")[0], 1)  # a failing read command writes no journal line
        self.assertEqual(len(self.journal()), n)
        code, out, _ = cli(self.root, "validate")
        self.assertEqual((code, out.strip()), (0, "OK"))

    def test_safe_mode_does_not_write(self):
        n = len(self.journal())
        env = {**os.environ, "ORC_SAFE_MODE": "1"}
        self.assertEqual(cli(self.root, "new", "task", "--title", "x", env=env)[0], 1)
        self.assertEqual(len(self.journal()), n)


# ---------------------------------------------------------------- F4

class TestEnvironmentFailure(Base):
    def setUp(self):
        super().setUp()
        r = self.root
        py = Path(PY).as_posix()
        (r / "env.py").write_text("print('Error: An Application Control policy has blocked this file.')\n"
                                  "raise SystemExit(1)\n", encoding="utf-8")
        (r / "bad.py").write_text("print('# fail 2'); raise SystemExit(1)\n", encoding="utf-8")
        with open(r / ".orchestra/config.yaml", "a", encoding="utf-8") as f:
            f.write(f"verify:\n  commands:\n    lint: '\"{py}\" env.py'\n    test: '\"{py}\" bad.py'\n")
        git(r, "init", "-q")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "i")
        self.t = oc.new_task(r, title="c", owner="coder", write_scope=["src/"])["id"]
        oc.advance(r, self.t, "dispatched")

    def test_classify(self):
        self.assertEqual(ov.classify("lint", 1, False, "An Application Control policy has blocked this file")[0], "env")
        self.assertEqual(ov.classify("lint", 1, False, "Cannot find native binding")[0], "env")
        self.assertEqual(ov.classify("build", 127, False, "")[:2], ("env", "ENVIRONMENT_FAILURE"))
        self.assertEqual(ov.classify("test", 1, False, "# fail 2")[:2], ("fail", "TEST_FAILURE"))
        self.assertEqual(ov.classify("lint", 1, False, "1 error")[:2], ("fail", "LINT_FAILURE"))
        self.assertEqual(ov.classify("test", None, True, "")[:2], ("fail", "TEST_FAILURE"))  # timeout ≠ environment
        self.assertEqual(ov.classify("test", 0, False, "")[0], "pass")

    def test_verify_separates_env_from_project_failure(self):
        oc.apply_envelope(self.root, oc.parse_envelope(
            f"msg: result\ntask_id: {self.t}\nstatus: done\n"
            "evidence: [{kind: lint, result: pass, summary: lint ok}, {kind: test, result: pass, summary: t ok}]\n"
            "summary: x\n"))
        code, out, _ = cli(self.root, "verify", self.t)
        self.assertEqual(code, 6, out)  # a real test failure ⇒ FAIL
        self.assertIn("- lint: orc env | self-reported pass ['EVD-0001'] | not checked (ENVIRONMENT_FAILURE)", out)
        self.assertIn("- test: orc fail | self-reported pass ['EVD-0002'] | MISMATCH", out)
        self.assertIn("ENVIRONMENT_FAILURE lint: blocked by Windows Application Control", out)
        lint = next(e for e in oc.load_registry(self.root, "evidence") if e["by"] == "orc" and e["kind"] == "lint")
        self.assertEqual(lint["result"], "info")
        self.assertTrue(lint["summary"].startswith("orc verify lint: ENVIRONMENT_FAILURE"))

    def test_only_env_is_incomplete(self):
        cfg = self.root / ".orchestra/config.yaml"
        oc.write_text(cfg, oc.read_text(cfg).replace("bad.py", "env.py"))
        t = oc.new_task(self.root, title="c2", owner="coder", write_scope=["src/"])["id"]
        oc.advance(self.root, t, "dispatched")
        code, out, _ = cli(self.root, "verify", t)
        self.assertEqual(code, 7, out)
        self.assertIn(f"VERIFY {t}: INCOMPLETE", out)


# ---------------------------------------------------------------- F5

if __name__ == "__main__":
    unittest.main()
