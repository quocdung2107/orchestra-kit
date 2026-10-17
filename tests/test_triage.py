"""orc triage (shadow) — rule table H2, modules, monotonic, lowering by the user's command, 3 stages in the K loop."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KIT = REPO / "plugins" / "orchestra"  # plugin root (cli/, hooks/, templates/…)
sys.path.insert(0, str(KIT / "cli"))
sys.path.insert(0, str(REPO / "tests"))
import orc_core as oc  # noqa: E402
import orc_guard as og  # noqa: E402
import orc_triage as ot  # noqa: E402
from test_workflow_k import Base as KBase  # noqa: E402

NO = {k: False for k in ot.FORM_KEYS}


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.cfg = {**ot.DEFAULTS, "l3": dict(ot.DEFAULTS["l3"]), "l2": dict(ot.DEFAULTS["l2"]),
                    "l0": dict(ot.DEFAULTS["l0"])}

    def lv(self, files, lines=None, form=None):
        return ot.classify(files, lines, form, self.cfg)[0]

    def test_rules(self):
        self.assertEqual(self.lv(["supabase/migrations/1.sql"], 3, NO), 3)            # risk path
        self.assertEqual(self.lv(["app/package.json"], 1, NO), 3)
        self.assertEqual(self.lv(["a/x.ts"], 1, {**NO, "schema_change": True}), 3)     # form L3
        self.assertEqual(self.lv([f"a/b/{i}.ts" for i in range(16)], 10, NO), 3)      # > 15 file
        self.assertEqual(self.lv(["a/b/x.ts"], 401, NO), 3)                            # > 400 lines
        # > 3 modules ⇒ L3 only when the diff is > 120 lines; otherwise the L2 rule (> 1 module)
        self.assertEqual(self.lv(["a/1/x", "b/1/x", "c/1/x", "d/1/x"], 4, NO), 2)
        self.assertEqual(self.lv(["a/1/x", "b/1/x", "c/1/x", "d/1/x"], 121, NO), 3)
        self.assertEqual(self.lv(["a/1/x", "b/1/x", "c/1/x", "d/1/x"], None, NO), 2)  # stage 2 does not know the lines yet
        self.assertEqual(self.lv(["a/b/x.ts"], 1, {**NO, "user_visible": True}), 2)    # form L2
        self.assertEqual(self.lv(["a/1/x", "b/1/x"], 4, NO), 2)                        # > 1 module
        self.assertEqual(self.lv([f"a/b/{i}" for i in range(6)], 4, NO), 2)           # > 5 file
        self.assertEqual(self.lv(["a/b/x"], 121, NO), 2)                               # > 120 lines
        self.assertEqual(self.lv(["a/b/x", "a/b/y", "a/b/z"], 30, NO), 1)             # 3 file 1 module
        self.assertEqual(self.lv(["a/b/x", "a/b/y"], 40, NO), 0)                       # qualifies for L0
        self.assertEqual(self.lv(["a/b/x", "a/b/y"], 41, NO), 1)                       # > 40 lines ⇒ not L0

    def test_risk_keywords(self):
        lv = lambda text: ot.classify(["a/b/x", "a/b/y"], 10, NO, self.cfg, text=text)[0]  # noqa: E731
        self.assertEqual(lv("B1 prevent CSV injection when exporting CSV"), 3)
        self.assertEqual(lv("Fix the LOGIN bug with OTP"), 3)          # case-insensitive
        self.assertEqual(lv("Add RLS to the orders table"), 3)
        self.assertEqual(lv("Paste {{ .Token }} into the email template"), 0)  # "token" is not in the list
        self.assertEqual(lv("Fix the authors page layout"), 0)         # ASCII matches whole words: authors ≠ auth
        self.assertEqual(lv("block Excel files > 20 MB"), 0)
        self.cfg["risk_keywords_l3"] = []
        self.assertEqual(lv("csv injection"), 0)                        # can be turned off in the config

    def test_never_default_l0(self):
        self.assertEqual(self.lv(None, None, None), 1)              # nothing at all
        self.assertEqual(self.lv(["a/b/x"], 3, None), 1)            # no form
        self.assertEqual(self.lv(["a/b/x"], 3, {"user_visible": False}), 1)   # form missing flags
        self.assertEqual(self.lv(None, None, NO), 1)                # full form but no paths

    def test_config_thresholds(self):
        self.cfg["l2"]["modules"] = 3
        self.assertEqual(self.lv(["a/1/x", "b/1/x"], 4, NO), 1)

    def test_module_of(self):
        self.assertEqual(ot.module_of("app/src/features/orders/Chat.tsx", ["app/src"]), "app/src/features/orders")
        self.assertEqual(ot.module_of("app/src/main.tsx", ["app/src"]), "app/src")
        self.assertEqual(ot.module_of("app/tests/a.test.ts", ["app/src"]), "app/tests")
        self.assertEqual(ot.module_of("README.md", []), ".")


class TestTriageCli(KBase):
    def test_monotonic_and_downgrade_needs_user(self):
        R = self.ok("new", "request", "--title", "root")
        form = self.root / "form.yaml"
        form.write_text("schema_change: true\n", encoding="utf-8")
        self.assertIn("L3", self.ok("triage", "run", R, "--stage", "1", "--form", str(form)))
        form.write_text("".join(f"{k}: false\n" for k in ot.FORM_KEYS), encoding="utf-8")
        out = self.ok("triage", "run", R, "--stage", "1", "--form", str(form))
        self.assertIn("L3 (computed L1, previously L3)", out)  # only goes up
        self.no("triage", "downgrade", R, "--to", "1", "--reason", "small")  # no prompt from the user yet
        self.say(f"orc:downgrade {R} L1 only edits comments")
        self.ok("triage", "downgrade", R, "--to", "1", "--reason", "only edits comments")
        self.assertIn(f"{R}: level 1", self.ok("triage", "show", R))
        self.assertIsNone(oc.find_entry(self.root, R)[2].get("level"))  # shadow: task.level not written
        out = self.ok("triage", "run", R, "--stage", "1", "--form", "-",
                      stdin="".join(f"{k}: false\n" for k in ot.FORM_KEYS))
        self.assertIn("stage 1", out)  # form through stdin (used by /auto)
        bad = self.root / "bad.yaml"
        bad.write_text("la: true\n", encoding="utf-8")
        self.no("triage", "run", R, "--stage", "1", "--form", str(bad))

    def test_three_stages_in_k_loop_shadow_only(self):
        R, UND, DEC, PLAN, C = self.to_planned()
        self.approve(R)  # still requires APPROVAL_REQUIRED: shadow triage does not open READY_FOR_IMPLEMENTATION
        self.implement(C)
        self.ok("advance", R, "--to", "VERIFYING")
        rows = [json.loads(l) for l in oc.journal_path(self.root).read_text(encoding="utf-8").splitlines()]
        tri = [r for r in rows if r.get("event") == "triage" and r.get("id") == R]
        self.assertEqual([r["stage"] for r in tri], [1, 2, 3])
        self.assertTrue(all(r["mode"] == "shadow" for r in tri))
        levels = [r["level"] for r in tri]
        self.assertEqual(levels, sorted(levels))  # monotonic
        self.assertEqual(tri[1]["files"], 1)      # write_scope app/ ⇒ the file already in git: app/status.txt
        self.assertGreaterEqual(tri[2]["files"], 1)
        self.assertIsNone(oc.find_entry(self.root, R)[2].get("level"))
        self.assertEqual(self.ok("validate"), "OK")


if __name__ == "__main__":
    unittest.main()
