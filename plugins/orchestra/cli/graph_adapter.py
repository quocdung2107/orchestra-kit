"""Graph Adapter — the abstraction between Orchestra and the knowledge-graph engine (currently: Graphify).

Principles:
- Optional: not configured / not installed / not built / failing ⇒ report a state, never break the workflow.
- Stdlib only. Graphify is called as a subprocess through the Python of its own venv, never imported.
- The engine is used only to BUILD the graph (`extract --code-only --out`). Reading/traversing is done by the adapter on
  graph.json (node-link format), so switching engines only means replacing build + load.
- Freshness is computed from MD5 of file contents against the engine's manifest (no mtime or git).
- The graph only suggests candidates/evidence; any state other than `fresh` means `authoritative = False`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

PINNED_VERSION = "0.9.76"
CODE_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".sql", ".py"}
SKIP_DIRS = {"node_modules", "dist", ".git", "graphify-out", "__pycache__"}
# Dependency edges (used for expansion/reverse traversal). "contains" is the structural edge file → symbol.
DEP_RELATIONS = {"calls", "indirect_call", "imports", "imports_from", "dynamic_import",
                 "references", "reads_from", "triggers", "uses", "inherits", "extends",
                 "implements", "re_exports"}
LOCK_STALE_SECONDS = 600
UPDATE_TIMEOUT = 300
STATES = ("none", "fresh", "stale", "degraded", "locked", "unavailable")


class GraphError(Exception):
    pass


def default_python() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share"))
    venv = base / "orchestra-kit" / "graphify-venv"
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def md5_file(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _norm(p: str) -> str:
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


class GraphAdapter:
    def __init__(self, project_root: Path, config: dict | None = None, runner=None):
        cfg = config or {}
        self.root = Path(project_root)
        self.roots = []
        for r in cfg.get("roots") or []:
            path = _norm(r["path"]) if isinstance(r, dict) else _norm(r)
            include = [_norm(i) for i in (r.get("include") if isinstance(r, dict) else None) or ["."]]
            self.roots.append({"path": path, "include": ["" if i in (".", "") else i for i in include]})
        self.enabled = bool(self.roots)
        self.python = Path(cfg.get("python") or default_python())
        self.cache = self.root / ".orchestra" / "cache" / "graph"
        self.max_results = int(cfg.get("max_results", 40))
        self.hub_degree = int(cfg.get("hub_degree", 25))
        self.runner = runner or subprocess.run

    # ------------------------------------------------------------ paths

    def _slug(self, r) -> str:
        return r["path"].replace("/", "__") or "root"

    def _out(self, r) -> Path:
        return self.cache / self._slug(r)

    def _engine_dir(self, r) -> Path:
        return self._out(r) / "graphify-out"

    def _lock(self) -> Path:
        return self.cache / ".lock"

    def _meta_path(self) -> Path:
        return self.cache / "meta.json"

    def _included(self, r, rel: str) -> bool:
        rel = _norm(rel)
        if Path(rel).suffix.lower() not in CODE_EXT:
            return False
        return any(i == "" or rel == i or rel.startswith(i + "/") for i in r["include"])

    # ------------------------------------------------------------ engine

    def _run(self, args, timeout):
        return self.runner([str(self.python), "-m", "graphify", *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)

    def engine_version(self) -> str | None:
        if not self.python.exists():
            return None
        try:
            r = self._run(["--version"], timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        parts = (r.stdout or "").split()
        return parts[-1] if parts else None

    def is_available(self) -> bool:
        return self.enabled and self.engine_version() is not None

    # ------------------------------------------------------------ status

    def current_files(self, r) -> dict:
        base = self.root / r["path"]
        out = {}
        if not base.is_dir():
            return out
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                full = Path(dirpath) / name
                rel = _norm(full.relative_to(base).as_posix())
                if self._included(r, rel):
                    out[rel] = md5_file(full)
        return out

    def _manifest(self, r):
        path = self._engine_dir(r) / "manifest.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return {_norm(k): (v or {}).get("ast_hash") for k, v in data.items() if self._included(r, k)}

    def _graph_json(self, r):
        path = self._engine_dir(r) / "graph.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _locked(self) -> bool:
        lock = self._lock()
        if not lock.exists():
            return False
        return time.time() - lock.stat().st_mtime < LOCK_STALE_SECONDS

    def status(self) -> dict:
        st = {"state": "none", "authoritative": False, "engine": "graphify", "files": 0,
              "nodes": 0, "changed": [], "problems": [], "fingerprint": None,
              "built_at": None, "version": None}
        if not self.enabled:
            st["problems"].append("graph.roots is not configured in config.yaml")
            return st
        if self._locked():
            st["state"] = "locked"
            st["problems"].append("the graph is being updated")
            return st
        meta = {}
        if self._meta_path().exists():
            try:
                meta = json.loads(self._meta_path().read_text(encoding="utf-8"))
            except ValueError:
                st["problems"].append("meta.json is broken")
        st["built_at"], st["version"] = meta.get("built_at"), meta.get("version")
        pairs, changed, missing_roots = [], [], 0
        try:
            for r in self.roots:
                current = self.current_files(r)
                manifest = self._manifest(r)
                graph = self._graph_json(r)
                if manifest is None or graph is None:
                    missing_roots += 1
                    continue
                prefix = f"{r['path']}/" if r["path"] else ""
                for rel in sorted(set(current) | set(manifest)):
                    if current.get(rel) != manifest.get(rel):
                        tag = "new" if rel not in manifest else "deleted" if rel not in current else "changed"
                        changed.append(f"{prefix}{rel} ({tag})")
                pairs += [f"{prefix}{k}:{v}" for k, v in sorted(current.items())]
                nodes = [n for n in graph.get("nodes", []) if n.get("source_file")]
                st["nodes"] += len(nodes)
                st["files"] += len(current)
                # SQL files exist but the graph has no SQL nodes
                if any(k.endswith(".sql") for k in current) and not any(
                        str(n.get("source_file", "")).endswith(".sql") for n in nodes):
                    st["problems"].append(f"{r['path']}: has .sql files but the graph has no SQL nodes "
                                          "(graphifyy[sql] missing?)")
        except (OSError, ValueError) as exc:
            st["state"] = "degraded"
            st["problems"].append(f"cannot read the graph: {exc}")
            return st
        if missing_roots == len(self.roots):
            st["problems"].append("the graph has not been built (orc graph update)")
            return st
        if missing_roots:
            st["problems"].append(f"{missing_roots} roots have no graph yet")
        # files the engine skipped right after a build (e.g. because of .gitignore) → not stale, missing
        skipped = set(meta.get("skipped") or [])
        missed = [c for c in changed if c.rsplit(" (", 1)[0] in skipped and c.endswith("(new)")]
        if missed:
            st["problems"].append(f"the graph skipped {len(missed)} files: " + ", ".join(m.rsplit(' (', 1)[0] for m in missed[:5]))
            changed = [c for c in changed if c not in missed]
        if meta.get("version") and meta["version"] != PINNED_VERSION:
            st["problems"].append(f"graph built with graphify {meta['version']}, the kit pins {PINNED_VERSION}")
        st["changed"] = changed
        st["fingerprint"] = hashlib.sha1("\n".join(pairs).encode("utf-8")).hexdigest()[:16]
        if st["problems"]:
            st["state"] = "degraded"
        elif changed:
            st["state"] = "stale"
        else:
            st["state"] = "fresh"
            st["authoritative"] = True
        return st

    # ------------------------------------------------------------ update

    def _acquire(self) -> None:
        self.cache.mkdir(parents=True, exist_ok=True)
        lock = self._lock()
        if lock.exists() and time.time() - lock.stat().st_mtime >= LOCK_STALE_SECONDS:
            lock.unlink()  # lock left behind by an interrupted run
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise GraphError("the graph is being updated by another session") from exc
        os.write(fd, f"{os.getpid()} {_dt.datetime.now().isoformat(timespec='seconds')}".encode())
        os.close(fd)

    def update(self, force: bool = False) -> dict:
        """Rebuild the graph when code/SQL changed (or when forced). Returns the status after the run."""
        if not self.enabled:
            return self.status()
        before = self.status()
        if before["state"] == "fresh" and not force:
            return before
        if before["state"] == "locked":
            raise GraphError("the graph is being updated by another session")
        version = self.engine_version()
        if version is None:
            raise GraphError(f"cannot run graphify with {self.python}")
        self._acquire()
        try:
            for r in self.roots:
                src = self.root / r["path"]
                if not src.is_dir():
                    raise GraphError(f"directory {r['path']} does not exist")
                args = ["extract", str(src), "--code-only", "--out", str(self._out(r))]
                if force:
                    args.append("--force")
                res = self._run(args, timeout=UPDATE_TIMEOUT)
                if res.returncode != 0:
                    tail = ((res.stderr or "") + (res.stdout or "")).strip().splitlines()[-3:]
                    raise GraphError(f"graphify extract {r['path']} failed: {' | '.join(tail)}")
            meta = {"version": version, "engine": "graphify",
                    "built_at": _dt.datetime.now().isoformat(timespec="seconds"),
                    "roots": self.roots, "skipped": []}
            self._meta_path().write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        finally:
            try:
                self._lock().unlink()
            except FileNotFoundError:
                pass
        # A file still "new" right after a build = skipped by the engine; recorded so status reports degraded
        after = self.status()
        skipped = [c.rsplit(" (", 1)[0] for c in after["changed"] if c.endswith("(new)")]
        if skipped:
            meta["skipped"] = skipped
            self._meta_path().write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            after = self.status()
        return after

    # ------------------------------------------------------------ view

    def view(self) -> dict:
        """Export an HTML map (node network + directory tree) for each root into the cache. For human viewing only."""
        st = self.status()
        if st["state"] in ("none", "locked"):
            raise GraphError(f"the graph is {st['state']}; run `orc graph update` first")
        pages = []
        for r in self.roots:
            gdir = self._engine_dir(r)
            graph = gdir / "graph.json"
            if not graph.exists():
                continue
            tree = gdir / "GRAPH_TREE.html"
            for args in (["export", "html", "--graph", str(graph)],
                         ["tree", "--graph", str(graph), "--output", str(tree), "--label", r["path"]]):
                res = self._run(args, timeout=120)
                if res.returncode != 0:
                    raise GraphError(f"exporting the map of {r['path']} failed: {(res.stderr or res.stdout).strip()[-200:]}")
            pages += [str(gdir / "graph.html"), str(tree)]
        return {"state": st["state"], "pages": pages,
                "note": "Open in a browser (needs network access to load the drawing library). The file lives in the cache, not committed."}

    # ------------------------------------------------------------ reading the graph

    def _load(self):
        st = self.status()
        if st["state"] not in ("fresh", "stale", "degraded"):
            return st, None
        nodes, out_e, in_e = {}, {}, {}
        for r in self.roots:
            graph = self._graph_json(r)
            if graph is None:
                continue
            prefix = f"{r['path']}/" if r["path"] else ""
            key = self._slug(r) + ":"
            for n in graph.get("nodes", []):
                f = _norm(n.get("source_file") or "")
                nodes[key + n["id"]] = {
                    "symbol": n.get("label"), "file": f"{prefix}{f}" if f else "",
                    "line": n.get("source_location") or "",
                    "in_scope": bool(f) and self._included(r, f)}
            for e in graph.get("links", []):
                s, t, rel = key + e["source"], key + e["target"], e.get("relation")
                out_e.setdefault(s, []).append((t, rel))
                in_e.setdefault(t, []).append((s, rel))
        # Degree counts only dependency edges: a large file containing many symbols ("contains") is not a hub
        degree = {k: sum(1 for _, rel in out_e.get(k, []) + in_e.get(k, []) if rel in DEP_RELATIONS)
                  for k in nodes}
        # SQL symbols with the same name in several migrations → only the one in the newest file is current
        latest = {}
        for k, n in nodes.items():
            if n["file"].endswith(".sql") and n["symbol"] and not n["symbol"].endswith(".sql"):
                if n["symbol"] not in latest or n["file"] > nodes[latest[n["symbol"]]]["file"]:
                    latest[n["symbol"]] = k
        for k, n in nodes.items():
            if n["file"].endswith(".sql") and n["symbol"] in latest and latest[n["symbol"]] != k:
                n["superseded_by"] = nodes[latest[n["symbol"]]]["file"]
        return st, {"nodes": nodes, "out": out_e, "in": in_e, "degree": degree}

    def _row(self, g, k, via=None, relation=None) -> dict:
        n = g["nodes"][k]
        row = {"file": n["file"], "symbol": n["symbol"], "line": n["line"]}
        if via:
            row["via"] = via
        if relation:
            row["relation"] = relation
        if n.get("superseded_by"):
            row["superseded_by"] = n["superseded_by"]
        if g["degree"].get(k, 0) > self.hub_degree:
            row["hub"] = True
        return row

    def _find(self, g, term: str) -> list:
        t = _norm(term).lower()
        exact = [k for k, n in g["nodes"].items() if n["in_scope"] and (
            (n["symbol"] or "").lower() in (t, t + "()") or n["file"].lower() == t)]
        if exact:
            return exact
        return [k for k, n in g["nodes"].items() if n["in_scope"] and (
            t in (n["symbol"] or "").lower() or t in n["file"].lower())]

    def _result(self, st, rows, limit=None) -> dict:
        limit = limit or self.max_results
        seen, uniq = set(), []
        for row in rows:
            key = (row["file"], row["symbol"])
            if key not in seen and row["file"]:
                seen.add(key)
                uniq.append(row)
        return {"state": st["state"], "authoritative": st["authoritative"],
                "truncated": len(uniq) > limit, "results": uniq[:limit],
                "note": "The graph is only a hint; it does not cover string-based RPC calls (TS → SQL)."}

    def _empty(self, st) -> dict:
        return {"state": st["state"], "authoritative": False, "truncated": False, "results": [],
                "problems": st.get("problems", [])}

    def query(self, terms, limit=None) -> dict:
        st, g = self._load()
        if g is None:
            return self._empty(st)
        rows = []
        seeds = []
        for term in terms:
            seeds += self._find(g, term)
        seeds = [s for s in dict.fromkeys(seeds) if not g["nodes"][s].get("superseded_by")] or seeds
        rows += [self._row(g, s, via="match") for s in seeds]
        for s in seeds:
            if g["degree"].get(s, 0) > self.hub_degree:
                continue  # do not expand from hubs (shared UI) to avoid exploding results
            for t, rel in g["out"].get(s, []) + g["in"].get(s, []):
                n = g["nodes"].get(t)
                if n and n["in_scope"] and rel in DEP_RELATIONS | {"contains"}:
                    rows.append(self._row(g, t, via=g["nodes"][s]["symbol"], relation=rel))
        return self._result(st, rows, limit)

    def affected(self, target: str, depth: int = 2, limit=None) -> dict:
        st, g = self._load()
        if g is None:
            return self._empty(st)
        starts = self._find(g, target)
        # file → the symbols it contains
        for s in list(starts):
            starts += [t for t, rel in g["out"].get(s, []) if rel == "contains"]
        rows, seen = [], set(starts)
        queue = deque((s, 0) for s in starts)
        while queue:
            k, d = queue.popleft()
            if d >= depth:
                continue
            for src, rel in g["in"].get(k, []):
                if rel not in DEP_RELATIONS or src in seen:
                    continue
                seen.add(src)
                if g["nodes"].get(src, {}).get("in_scope"):
                    rows.append(self._row(g, src, via=g["nodes"][k]["symbol"], relation=rel))
                if g["degree"].get(src, 0) <= self.hub_degree:
                    queue.append((src, d + 1))
        return self._result(st, rows, limit)

    def explain(self, target: str, limit=None) -> dict:
        st, g = self._load()
        if g is None:
            return self._empty(st)
        starts = self._find(g, target)[:3]
        rows = []
        for s in starts:
            rows.append(self._row(g, s, via="match"))
            for t, rel in g["out"].get(s, []):
                if t in g["nodes"]:
                    rows.append(self._row(g, t, via=f"{g['nodes'][s]['symbol']} →", relation=rel))
            for t, rel in g["in"].get(s, []):
                if t in g["nodes"]:
                    rows.append(self._row(g, t, via=f"→ {g['nodes'][s]['symbol']}", relation=rel))
        return self._result(st, rows, limit)

    def path(self, a: str, b: str) -> dict:
        st, g = self._load()
        if g is None:
            return self._empty(st)
        starts, goals = self._find(g, a), set(self._find(g, b))
        prev = {s: None for s in starts}
        queue = deque(starts)
        hit = None
        while queue and hit is None:
            k = queue.popleft()
            if k in goals:
                hit = k
                break
            for t, rel in g["out"].get(k, []) + g["in"].get(k, []):
                if t not in prev and (rel in DEP_RELATIONS or rel == "contains"):
                    prev[t] = k
                    queue.append(t)
        rows = []
        while hit is not None:
            rows.append(self._row(g, hit))
            hit = prev[hit]
        res = self._result(st, list(reversed(rows)), limit=50)
        if not rows:
            res["note"] = ("No path in the graph. Do not conclude 'no dependency': "
                           "the graph does not cover string-based RPC.")
        return res


def main(argv=None) -> int:  # pragma: no cover - convenience for manual runs
    print("Use it through `orc graph ...`", file=sys.stderr)
    return 1
