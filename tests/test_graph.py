"""Tests of the Graph Adapter + orc graph/ctx with a FAKE engine (graphify not needed)."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KIT = REPO / "plugins" / "orchestra"  # plugin root (cli/, hooks/, templates/…)
sys.path.insert(0, str(KIT / "cli"))

import graph_adapter as ga  # noqa: E402
import orc_core as oc  # noqa: E402


def node(i, label, f, line="L1"):
    return {"id": i, "label": label, "source_file": f, "source_location": line}


def link(s, t, rel):
    return {"source": s, "target": t, "relation": rel}


APP_GRAPH = {
    "nodes": [
        node("fmt", "formatter.ts", "src/formatter.ts"),
        node("csv", "csvSafe()", "src/formatter.ts", "L3"),
        node("page", "Page.tsx", "src/Page.tsx"),
        node("exp", "exportFile()", "src/Page.tsx", "L9"),
        node("ui", "Button()", "src/ui.tsx"),
        node("t", "csv.test.ts", "tests/csv.test.ts"),
        node("ext", "ref_xlsx", ""),
    ],
    "links": [
        link("fmt", "csv", "contains"), link("page", "exp", "contains"),
        link("page", "fmt", "imports"), link("exp", "csv", "calls"),
        link("t", "csv", "calls"), link("exp", "ui", "calls"), link("page", "ext", "imports"),
    ] + [link(f"u{i}", "ui", "calls") for i in range(30)],
}
APP_GRAPH["nodes"] += [node(f"u{i}", f"Screen{i}()", f"src/s{i}.tsx") for i in range(30)]

SQL_GRAPH = {
    "nodes": [node("m1", "public.create_order()", "001_init.sql", "L10"),
              node("m2", "public.create_order()", "002_fields.sql", "L5")],
    "links": [],
}


class FakeEngine:
    """Simulates `python -m graphify --version | extract <src> --code-only --out <out>`."""

    def __init__(self, graphs, omit=(), fail=False, version="0.9.76"):
        self.graphs, self.omit, self.fail, self.version = graphs, set(omit), fail, version
        self.calls = []

    def __call__(self, cmd, **kw):
        args = cmd[3:]
        self.calls.append(args)
        if args[0] == "--version":
            return subprocess.CompletedProcess(cmd, 0, f"graphify {self.version}\n", "")
        if args[0] in ("export", "tree"):
            if "--output" in args:
                out = Path(args[args.index("--output") + 1])
            else:
                out = Path(args[args.index("--graph") + 1]).parent / "graph.html"
            out.write_text("<html></html>", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "boom\n")
        src, out = Path(args[1]), Path(args[args.index("--out") + 1]) / "graphify-out"
        out.mkdir(parents=True, exist_ok=True)
        manifest = {}
        for p in src.rglob("*"):
            rel = p.relative_to(src).as_posix()
            if p.is_file() and p.suffix in ga.CODE_EXT and rel not in self.omit:
                manifest[rel] = {"ast_hash": hashlib.md5(p.read_bytes()).hexdigest()}
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        key = src.name
        (out / "graph.json").write_text(json.dumps(self.graphs.get(key, {"nodes": [], "links": []})),
                                        encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        oc.cmd_init(self.root, "demo", "g")
        files = {"app/src/formatter.ts": "export const csvSafe = 1\n",
                 "app/src/Page.tsx": "page\n", "app/src/ui.tsx": "ui\n",
                 "app/tests/csv.test.ts": "test\n", "app/vite.config.ts": "cfg\n",
                 "app/node_modules/x/index.js": "x\n",
                 "db/001_init.sql": "create function a\n", "db/002_fields.sql": "create function b\n"}
        files.update({f"app/src/s{i}.tsx": f"s{i}\n" for i in range(30)})
        for rel, text in files.items():
            oc.write_text(self.root / rel, text)
        self.cfg = {"python": sys.executable, "max_files": 3, "roots": [
            {"path": "app", "include": ["src", "tests"]}, {"path": "db"}]}
        self.engine = FakeEngine({"app": APP_GRAPH, "db": SQL_GRAPH})

    def tearDown(self):
        self.tmp.cleanup()

    def adapter(self, engine=None, cfg=None):
        return ga.GraphAdapter(self.root, self.cfg if cfg is None else cfg, runner=engine or self.engine)

    def configure(self):
        path = self.root / ".orchestra" / "config.yaml"
        cfg = oc.load_config(self.root)
        cfg["graph"] = self.cfg
        oc.write_text(path, oc.dump_yaml(cfg))


class TestStatusAndUpdate(Base):
    def test_not_configured_is_none_and_harmless(self):
        st = ga.GraphAdapter(self.root, None).status()
        self.assertEqual((st["state"], st["authoritative"]), ("none", False))
        self.assertEqual(oc.graph_brief_line(self.root), "")
        self.assertNotIn("Graph:", oc.brief_text(self.root))

    def test_missing_engine_never_breaks(self):
        cfg = dict(self.cfg, python=str(self.root / "missing" / "python.exe"))
        ad = ga.GraphAdapter(self.root, cfg)
        self.assertFalse(ad.is_available())
        self.assertEqual(ad.status()["state"], "none")
        with self.assertRaises(ga.GraphError):
            ad.update()
        self.assertEqual(ad.query(["csv"])["results"], [])

    def test_build_fresh_then_stale_then_fresh(self):
        ad = self.adapter()
        st = ad.update()
        self.assertEqual(st["state"], "fresh", st)
        self.assertTrue(st["authoritative"])
        self.assertEqual(st["files"], 36)  # 34 app files in src/tests + 2 sql; vite.config and node_modules excluded
        calls = len(self.engine.calls)
        self.assertEqual(ad.update()["state"], "fresh")
        self.assertEqual(len(self.engine.calls), calls, "fresh means the engine does not run")
        oc.write_text(self.root / "app/src/formatter.ts", "export const csvSafe = 2\n")
        st = ad.status()
        self.assertEqual(st["state"], "stale")
        self.assertIn("app/src/formatter.ts (changed)", st["changed"])
        self.assertFalse(ad.query(["csvSafe"])["authoritative"])
        self.assertEqual(ad.update()["state"], "fresh")

    def test_docs_change_does_not_make_stale(self):
        ad = self.adapter()
        ad.update()
        oc.write_text(self.root / "app/README.md", "docs\n")
        oc.write_text(self.root / "app/vite.config.ts", "cfg2\n")  # outside include
        self.assertEqual(ad.status()["state"], "fresh")

    def test_f1_engine_skips_files_is_degraded(self):
        ad = self.adapter(FakeEngine({"app": APP_GRAPH, "db": SQL_GRAPH},
                                     omit={f"src/s{i}.tsx" for i in range(30)}))
        st = ad.update()
        self.assertEqual(st["state"], "degraded", st)
        self.assertTrue(any("skipped 30 files" in p for p in st["problems"]))

    def test_f2_sql_without_nodes_is_degraded(self):
        ad = self.adapter(FakeEngine({"app": APP_GRAPH, "db": {"nodes": [], "links": []}}))
        st = ad.update()
        self.assertEqual(st["state"], "degraded")
        self.assertTrue(any(".sql" in p for p in st["problems"]))

    def test_engine_failure_releases_lock(self):
        ad = self.adapter(FakeEngine({}, fail=True))
        with self.assertRaises(ga.GraphError):
            ad.update()
        self.assertFalse((ad.cache / ".lock").exists())

    def test_lock_blocks_update_and_reads(self):
        ad = self.adapter()
        ad.update()
        ad._acquire()
        self.assertEqual(ad.status()["state"], "locked")
        with self.assertRaises(ga.GraphError):
            ad.update(force=True)
        self.assertEqual(ad.query(["csv"])["results"], [])
        old = time.time() - ga.LOCK_STALE_SECONDS - 5
        os.utime(ad.cache / ".lock", (old, old))
        self.assertEqual(ad.status()["state"], "fresh")

    def test_version_mismatch_is_degraded(self):
        ad = self.adapter(FakeEngine({"app": APP_GRAPH, "db": SQL_GRAPH}, version="0.9.99"))
        st = ad.update()
        self.assertEqual(st["state"], "degraded")
        self.assertTrue(any("0.9.99" in p for p in st["problems"]))


class TestReads(Base):
    def setUp(self):
        super().setUp()
        self.ad = self.adapter()
        self.ad.update()

    def test_query_expands_but_not_from_hub_and_skips_out_of_scope(self):
        res = self.ad.query(["csvSafe"])
        files = {r["file"] for r in res["results"]}
        self.assertIn("app/src/formatter.ts", files)
        self.assertIn("app/tests/csv.test.ts", files)
        self.assertIn("app/src/Page.tsx", files)
        hub = self.ad.query(["Button"])
        self.assertTrue(hub["results"][0]["hub"])
        self.assertEqual(len(hub["results"]), 1, "no expansion from hubs")
        self.assertNotIn("", {r["file"] for r in res["results"]})

    def test_affected_reverse(self):
        res = self.ad.affected("csvSafe()")
        files = {r["file"] for r in res["results"]}
        self.assertEqual(files, {"app/src/Page.tsx", "app/tests/csv.test.ts"})

    def test_affected_file_target_and_limit(self):
        res = self.ad.affected("app/src/ui.tsx", limit=5)
        self.assertTrue(res["truncated"])
        self.assertEqual(len(res["results"]), 5)

    def test_f3_sql_redefinition_prefers_latest_migration(self):
        res = self.ad.query(["create_order"])
        self.assertEqual([r["file"] for r in res["results"]], ["db/002_fields.sql"])
        exp = self.ad.explain("public.create_order()")
        old = [r for r in exp["results"] if r["file"] == "db/001_init.sql"]
        self.assertEqual(old[0]["superseded_by"], "db/002_fields.sql")

    def test_view_exports_pages_per_root(self):
        res = self.ad.view()
        self.assertEqual(len(res["pages"]), 4)
        self.assertTrue(all(Path(p).exists() for p in res["pages"]))
        self.assertTrue(all(".orchestra" in p for p in res["pages"]))

    def test_path_and_no_path_note(self):
        res = self.ad.path("csv.test.ts", "exportFile()")
        self.assertGreaterEqual(len(res["results"]), 2)
        none = self.ad.path("csvSafe()", "create_order")
        self.assertEqual(none["results"], [])
        self.assertIn("Do not conclude", none["note"])


class TestOrcIntegration(Base):
    def setUp(self):
        super().setUp()
        self.configure()
        self.task = oc.new_task(self.root, title="B2")["id"]

    def write_ctx(self, source, paths):
        ctx = oc.new_context(self.root, self.task, "ctx")["id"]
        files = "\n".join(f"  - path: {p}\n    reason: why" for p in paths)
        body = f"# ctx\n```yaml orc-context\nsource: {source}\ntruncated: false\nfiles:\n{files}\n```\n"
        oc.write_text(oc.content_path(self.root, ctx), body)
        return ctx

    def test_fallback_ctx_without_graph_and_check(self):
        ctx = self.write_ctx("fallback", ["app/src/formatter.ts", "app/src/Page.tsx"])
        e = oc.ctx_seal(self.root, ctx)
        self.assertEqual((e["source"], e["graph_state"]), ("fallback", "none"))
        self.assertEqual(oc.ctx_check(self.root, ctx), [])
        oc.write_text(self.root / "app/src/Page.tsx", "changed\n")
        self.assertEqual(oc.ctx_check(self.root, ctx), ["app/src/Page.tsx (changed)"])
        self.assertEqual(oc.validate(self.root), [])

    def test_graph_source_requires_fresh_graph(self):
        ctx = self.write_ctx("graph", ["app/src/formatter.ts"])
        with self.assertRaises(oc.OrcError):
            oc.ctx_seal(self.root, ctx)  # graph not built yet

    def test_budget_and_missing_file(self):
        with self.assertRaises(oc.OrcError):
            oc.ctx_seal(self.root, self.write_ctx("fallback", [f"app/src/s{i}.tsx" for i in range(4)]))
        with self.assertRaises(oc.OrcError):
            oc.ctx_seal(self.root, self.write_ctx("fallback", ["app/src/missing.ts"]))

    def test_validate_rejects_bad_ctx_fields(self):
        ctx = self.write_ctx("fallback", ["app/src/ui.tsx"])
        oc.ctx_seal(self.root, ctx)
        items = oc.load_registry(self.root, "context")
        items[0]["graph_state"] = "xanh"
        oc.save_registry(self.root, "context", items)
        self.assertTrue(any("graph_state" in e for e in oc.validate(self.root)))

    def test_brief_shows_graph_state(self):
        self.assertIn("Graph: none", oc.brief_text(self.root))

    def test_init_gitignores_cache(self):
        self.assertIn("cache/", (self.root / ".orchestra/.gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
