"""Bridge: a legacy global hook yields to the plugin in projects that enabled the orchestra plugin.

Run: python -m unittest tests.test_bridge -v
"""

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

import orc_core as oc  # noqa: E402

PY = sys.executable


def hook(name, payload, plugin_root=None):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    if plugin_root:
        env["CLAUDE_PLUGIN_ROOT"] = plugin_root
    r = subprocess.run([PY, str(KIT / "hooks" / name)], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env)
    return r.returncode, r.stdout.strip(), r.stderr


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        oc.cmd_init(self.root, "demo", "Try the bridge")
        (self.root / ".claude").mkdir()
        # open an S-ID so stop_check has something to block when the hook really runs
        oc.handoff_open(self.root, "orchestrator", key="s1")
        self.payload = {"cwd": str(self.root / ".orchestra"), "session_id": "s1"}

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, name, text):
        (self.root / ".claude" / name).write_text(text, encoding="utf-8")

    def assert_runs(self, plugin_root=None):
        code, out, err = hook("session_start", self.payload, plugin_root)
        self.assertEqual(code, 0, err)
        self.assertIn("Orchestra brief", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        code, out, err = hook("stop_check", self.payload, plugin_root)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["decision"], "block")

    def assert_silent(self):
        for name in ("session_start", "stop_check"):
            code, out, err = hook(name, self.payload)
            self.assertEqual((code, out), (0, ""), (name, err))

    def test_plugin_not_enabled_legacy_hook_runs(self):
        self.settings("settings.json", json.dumps({"enabledPlugins": {"orchestra@orchestra-kit": False,
                                                                      "other@x": True}}))
        self.assertFalse(oc.plugin_enabled(self.root))
        self.assert_runs()

    def test_plugin_enabled_legacy_hook_yields(self):
        self.settings("settings.local.json", json.dumps({"enabledPlugins": {"orchestra@orchestra-kit": True}}))
        self.assertTrue(oc.plugin_enabled(self.root / ".orchestra"))
        self.assert_silent()

    def test_plugin_enabled_plugin_hook_runs(self):
        self.settings("settings.json", json.dumps({"enabledPlugins": {"orchestra@orchestra-kit": True}}))
        self.assert_runs(plugin_root=str(KIT))

    def test_broken_json_counts_as_not_enabled(self):
        self.settings("settings.json", '{"enabledPlugins": {"orchestra@orchestra-kit": true')
        self.settings("settings.local.json", "not json")
        self.assertFalse(oc.plugin_enabled(self.root))
        self.assert_runs()


class TestPluginHooks(unittest.TestCase):
    """The plugin's hooks/hooks.json: all 4 events, pointing at real files, no-op without .orchestra/."""

    def test_hooks_json_complete_and_noop_outside_orchestra(self):
        cfg = json.loads((KIT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        want = {"SessionStart": "session_start", "Stop": "stop_check",
                "UserPromptSubmit": "prompt_log", "PreToolUse": "guard"}
        self.assertEqual(set(cfg), set(want))
        self.assertEqual(cfg["PreToolUse"][0]["matcher"], "*")
        with tempfile.TemporaryDirectory() as d:
            for event, name in want.items():
                cmd = cfg[event][0]["hooks"][0]["command"]
                self.assertEqual(cmd, f'python "${{CLAUDE_PLUGIN_ROOT}}/hooks/{name}"')
                self.assertTrue((KIT / "hooks" / name).is_file(), name)
                payload = {"cwd": d, "session_id": "x", "prompt": "orc:approve TASK-0001 ABC",
                           "tool_name": "Write", "tool_input": {"file_path": f"{d}/a.txt"}}
                code, out, err = hook(name, payload, plugin_root=str(KIT))
                self.assertEqual((code, out), (0, ""), (name, err))
            self.assertFalse((Path(d) / ".orchestra").exists())


if __name__ == "__main__":
    unittest.main()
