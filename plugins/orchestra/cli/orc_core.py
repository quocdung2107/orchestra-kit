"""orc — the deterministic CLI of orchestra-kit.

All Orchestrator logic that needs no LLM lives here: issuing IDs, writing the registry
(.orchestra/state/, written only by orc), picking ready tasks, checking blocked_by,
routing review results by failure_class, validation, brief, handoff.

Python stdlib + PyYAML only. PyYAML is imported lazily so hooks can no-op
in directories without .orchestra/ even when PyYAML is not installed.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = KIT_DIR / "templates"
ORC_DIR = ".orchestra"

KINDS = ("task", "artifact", "decision", "context", "evidence")
PREFIX = {"task": "TASK", "artifact": "ART", "decision": "DEC",
          "context": "CTX", "evidence": "EVD", "session": "S"}
KIND_OF_PREFIX = {v: k for k, v in PREFIX.items()}
REGISTRY_FILE = {"task": "tasks", "artifact": "artifacts", "decision": "decisions",
                 "context": "contexts", "evidence": "evidence"}
# Directory holding the content files (outside state/) of each kind.
CONTENT_DIR = {"artifact": "artifacts", "decision": "decisions",
               "context": "contexts", "evidence": "evidence", "session": "sessions"}

ID_RE = re.compile(r"^(TASK|ART|DEC|CTX|EVD|S)-(\d{4,})$")
ACC_RE = re.compile(r"^(ART-\d{4,})#(AC-\d+)$")

TASK_STATUS = ("pending", "ready", "dispatched", "in_review", "done", "failed", "blocked",
               "aborted")  # aborted: cancelled by the user with `orc:abort`
OWNER_RE = re.compile(r"^(orchestrator|researcher|reviewer|coder(-\d+)?)$")
ART_TYPES = ("plan", "research", "code_patch", "review_report")
ART_STATUS = ("draft", "accepted", "rejected")
DEC_STATUS = ("proposed", "accepted", "superseded")
EVD_KINDS = ("test", "lint", "build", "manual", "other", "typecheck", "scope")
ORC_ONLY_KINDS = ("typecheck", "scope")   # created only by `orc verify` (evidence boundary)
EVD_BY = ("agent", "orc")
EVD_RESULTS = ("pass", "fail", "info")
CTX_SOURCES = ("manual", "graph", "fallback")
GRAPH_STATES = ("none", "fresh", "stale", "degraded", "locked", "unavailable")
CTX_MAX_FILES = 12
FAILURE_CLASSES = ("IMPL_BUG", "SPEC_GAP", "DECISION_CONFLICT", "REGRESSION",
                   # from orc verify and the K gates
                   "TEST_FAILURE", "LINT_FAILURE", "BUILD_FAILURE", "TYPECHECK_FAILURE", "SCOPE_VIOLATION",
                   "ENVIRONMENT_FAILURE", "APPROVAL_MISSING")
ENV_STATUS = ("done", "partial", "blocked", "failed")
HANDOFF_RESULTS = ("done", "partial", "blocked")
HANDOFF_SECTIONS = ("Done", "Files changed", "Decisions made",
                    "Open questions", "Next steps")
PLACEHOLDER = "_(fill in)_"
SUMMARY_MAX = 200
MAX_ATTEMPTS = 2          # attempts > 2 → ask the user
MAX_STOP_BLOCKS = 3       # prevents an endless loop in the Stop hook
BRIEF_MAX_CHARS = 6000    # ~1.5k token
LOCK_WAIT = 10.0          # seconds to wait for the state lock
LOCK_STALE = 120.0        # a lock older than this is considered abandoned (dead process)
JOURNAL_FILE = "journal.jsonl"
# State fields: `orc set` must not change them; only `orc advance` / `orc apply` / `orc next` do
STATE_FIELDS = ("status", "level", "risk_flags", "attempts", "failure_class", "needs_user", "resume_status",
                "parent", "workflow", "recovery_action")
# Manual transitions (basic workflow rules) through `orc advance` (the K state machine replaces them for K requests)
ADVANCE_MOVES = {
    "task": {("pending", "dispatched"), ("ready", "dispatched"), ("dispatched", "ready"),
             ("blocked", "ready"), ("failed", "ready"), ("done", "dispatched")},
    "artifact": {("draft", "accepted"), ("draft", "rejected")},
    # DEC: no manual transition — accepted only through `orc approve` with the user's token
}

FIELDS = {
    "task": {
        "id": None, "title": "", "status": "pending", "owner": "coder",
        "blocked_by": [], "context": [], "inputs": [], "outputs": [],
        "acceptance": [], "write_scope": [], "attempts": 0, "summary": "",
        "failure_class": None, "needs_user": False, "resume_status": None,
        "level": None, "risk_flags": [],
        "parent": None, "workflow": None, "recovery_action": None,
    },
    "artifact": {
        "id": None, "type": "plan", "task": None, "version": 1, "supersedes": None,
        "status": "draft", "summary": "", "depends_on": [], "paths": [],
    },
    "decision": {"id": None, "status": "proposed", "scope": "project",
                 "summary": "", "supersedes": None},
    "context": {"id": None, "task": None, "summary": "", "paths": [],
                "source": "manual", "graph_state": "none", "fingerprint": None, "truncated": False},
    "evidence": {"id": None, "task": None, "kind": "test", "result": "info",
                 "summary": "", "paths": [], "by": "agent"},
}
# Fields added later: older entries without them are still valid (defaults in FIELDS)
OPTIONAL_FIELDS = {"evidence": {"by"},
                   "task": {"level", "risk_flags", "parent", "workflow", "recovery_action"}}  # parent/workflow/recovery_action: K; level/risk_flags: triage
LIST_FIELDS = {k: {f for f, d in v.items() if isinstance(d, list)} for k, v in FIELDS.items()}
SESSION_FIELDS = ("id", "task", "role", "result")


class OrcError(Exception):
    """An error the user/agent must see; the CLI prints it and exits with code 1."""


def _yaml():
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise OrcError("PyYAML is missing. Install it: python -m pip install --user pyyaml") from exc
    return yaml


def today() -> str:
    return _dt.date.today().isoformat()


# ---------------------------------------------------------------- location & IO

def find_root(start) -> Path | None:
    """Find the project directory containing .orchestra/config.yaml, walking up from start."""
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / ORC_DIR / "config.yaml").is_file():
            return d
    return None


PLUGIN_PREFIX = "orchestra@"


def plugin_enabled(start) -> bool:
    """Whether the project (nearest `.claude/` directory walking up from start) has enabled the orchestra plugin.

    Enabled = settings.json or settings.local.json has enabledPlugins["orchestra@…"] == true.
    Broken JSON or a missing file counts as not enabled.
    """
    try:
        p = Path(start).resolve()
    except Exception:
        return False
    for d in (p, *p.parents):
        cdir = d / ".claude"
        if not cdir.is_dir():
            continue
        for name in ("settings.json", "settings.local.json"):
            try:
                data = json.loads((cdir / name).read_text(encoding="utf-8"))
                plugins = data.get("enabledPlugins") or {}
                if any(str(k).startswith(PLUGIN_PREFIX) and v is True for k, v in plugins.items()):
                    return True
            except Exception:
                continue
        return False
    return False


def legacy_hook_shadowed(cwd) -> bool:
    """Bridge for the legacy (pre-plugin) install: a global hook without CLAUDE_PLUGIN_ROOT yields to the
    plugin in projects that enabled the orchestra plugin, so hooks do not run twice."""
    return not os.environ.get("CLAUDE_PLUGIN_ROOT") and plugin_enabled(cwd)


def require_root(start=".") -> Path:
    root = find_root(start)
    if root is None:
        raise OrcError("No .orchestra/ in this directory or any parent. Run `orc init`.")
    return root


def orc(root: Path) -> Path:
    return root / ORC_DIR


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    """Atomic write: temp file in the same directory, then os.replace. A failure midway keeps the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for i in range(50):  # Windows: the target may be open for reading by another process
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if i == 49:
                    raise
                time.sleep(0.02)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextlib.contextmanager
def state_lock(root: Path):
    """Inter-process lock for commands that write state. Not reentrant; held at CLI-command level."""
    path = orc(root) / "runtime" / "state.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > LOCK_STALE:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                continue
            if time.monotonic() > deadline:
                raise OrcError(f"state is locked by another orc command ({path.name}); try again later") from None
            time.sleep(0.05)
            continue
        os.write(fd, f"{os.getpid()} {_dt.datetime.now().isoformat(timespec='seconds')}\n".encode())
        os.close(fd)
        break
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def safe_mode() -> bool:
    """Set ORC_SAFE_MODE=1 before opening the editor to repair Orchestra itself."""
    return os.environ.get("ORC_SAFE_MODE", "").strip().lower() in ("1", "true", "on", "yes")


# Commands that change workflow/project state: refused in SAFE_MODE (cmd, action, or None = every action)
SAFE_BLOCKED = {("new", None), ("set", None), ("advance", None), ("apply", None), ("next", None),
                ("approve", None), ("verify", None), ("init", None), ("approval", "request"),
                ("resume", None), ("abort", None), ("triage", "run"), ("triage", "downgrade"),
                ("handoff", "open"), ("ctx", "seal"), ("docs", "index")}


# ---------------------------------------------------------------- journal

_JOURNAL = {"actor": "api", "evidence": [], "extra": {}}


@contextlib.contextmanager
def journal_context(actor: str | None = None, evidence=None, extra=None):
    """Set actor/evidence (and extra fields, e.g. checkpoint) for journal lines written inside this block."""
    old = dict(_JOURNAL)
    if actor is not None:
        _JOURNAL["actor"] = actor
    if evidence is not None:
        _JOURNAL["evidence"] = list(evidence)
    if extra is not None:
        _JOURNAL["extra"] = dict(extra)
    try:
        yield
    finally:
        _JOURNAL.update(old)


def journal_path(root: Path) -> Path:
    return orc(root) / "state" / JOURNAL_FILE


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def append_journal(root: Path, rows: list) -> None:
    """Append state-transition lines; each line carries the sha256 of the previous line (detects manual edits)."""
    if not rows:
        return
    path = journal_path(root)
    text = read_text(path) if path.exists() else ""
    lines = [l for l in text.splitlines() if l.strip()]
    prev = _line_hash(lines[-1]) if lines else "0" * 64
    ts = _dt.datetime.now().isoformat(timespec="microseconds")
    for r in rows:
        line = json.dumps({"ts": ts, **_JOURNAL["extra"], **r, "actor": _JOURNAL["actor"],
                           "evidence": _JOURNAL["evidence"], "prev": prev},
                          ensure_ascii=False, sort_keys=True)
        lines.append(line)
        prev = _line_hash(line)
    write_text(path, "\n".join(lines) + "\n")


def write_active(root: Path) -> None:
    """`runtime/active.json` for the guard (JSON, not YAML): running tasks + their scope + artifacts."""
    tasks = load_registry(root, "task")
    arts = load_registry(root, "artifact")
    active = []
    for t in tasks:
        if isinstance(t, dict) and t.get("status") in ("dispatched", "in_review"):
            active.append({"id": t["id"], "status": t["status"], "owner": t.get("owner"),
                           "write_scope": as_list(t.get("write_scope")),
                           "artifacts": [p for a in arts if isinstance(a, dict) and a.get("task") == t["id"]
                                         for p in as_list(a.get("paths"))]})
    # .orchestra/ files an agent may write (exceptions to deny_always): artifacts/contexts of unfinished tasks,
    # the handoff of the open session (result pending). Everything else in .orchestra/ is written only by `orc`.
    open_tasks = {t["id"] for t in tasks if isinstance(t, dict) and t.get("status") not in ("done", "DONE",
                                                                                         "INTEGRATED", "ABORTED", "aborted")}
    writable = [f"{ORC_DIR}/artifacts/{a['id']}.md" for a in arts
                if isinstance(a, dict) and a.get("task") in open_tasks]
    writable += [f"{ORC_DIR}/contexts/{c['id']}.md" for c in load_registry(root, "context")
                 if isinstance(c, dict) and c.get("task") in open_tasks]
    for sid in session_ids(root):
        try:
            fm, _ = split_frontmatter(read_text(content_path(root, sid)))
        except Exception:  # noqa: BLE001 - broken frontmatter: not writable
            continue
        if (fm or {}).get("result") == "pending":
            writable.append(f"{ORC_DIR}/sessions/{sid}.md")
    protected = as_list((load_config(root).get("guard") or {}).get("protected_paths"))
    write_text(orc(root) / "runtime" / "active.json",
               json.dumps({"version": 2, "tasks": active, "writable": sorted(writable), "protected": protected},
                          ensure_ascii=False, indent=1) + "\n")


def journal_errors(root: Path) -> list:
    path = journal_path(root)
    if not path.exists():
        return []
    errs, prev = [], "0" * 64
    for n, line in enumerate((l for l in read_text(path).splitlines() if l.strip()), 1):
        try:
            row = json.loads(line)
        except ValueError:
            errs.append(f"{JOURNAL_FILE}:{n}: not JSON")
            prev = _line_hash(line)
            continue
        if not isinstance(row, dict) or row.get("prev") != prev:
            errs.append(f"{JOURNAL_FILE}:{n}: hash chain broken (previous line edited/deleted?)")
        prev = _line_hash(line)
    return errs


def dump_yaml(data) -> str:
    return _yaml().safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


def load_yaml_file(path: Path):
    return _yaml().safe_load(read_text(path)) if path.exists() else None


def load_config(root: Path) -> dict:
    return load_yaml_file(orc(root) / "config.yaml") or {}


def registry_path(root: Path, kind: str) -> Path:
    return orc(root) / "state" / f"{REGISTRY_FILE[kind]}.yaml"


def load_registry(root: Path, kind: str) -> list:
    data = load_yaml_file(registry_path(root, kind))
    if data is None:
        return []
    if not isinstance(data, list):
        raise OrcError(f"{registry_path(root, kind).name}: must be a YAML list")
    return data


REGISTRY_HEADER = "# Registry managed by `orc`. Do not edit by hand (single writer).\n"


def save_registry(root: Path, kind: str, items: list) -> None:
    """Write a registry; every `status` change (including new entries) goes to the journal — in one place."""
    old = {}
    if "status" in FIELDS[kind]:
        try:
            old = {e.get("id"): e.get("status") for e in load_registry(root, kind) if isinstance(e, dict)}
        except Exception:  # broken registry: still writable, the journal treats entries as new
            old = {}
    write_text(registry_path(root, kind), REGISTRY_HEADER + dump_yaml(items or []))
    if kind in ("task", "artifact"):
        write_active(root)
    if "status" in FIELDS[kind]:
        append_journal(root, [
            {"id": e.get("id"), "from": old.get(e.get("id")), "to": e.get("status")}
            for e in items or [] if isinstance(e, dict)
            and (e.get("id") not in old or old[e.get("id")] != e.get("status"))])


def split_frontmatter(text: str):
    """Return (frontmatter dict | None, body)."""
    if not text.startswith("---"):
        return None, text
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm = _yaml().safe_load("".join(lines[1:i])) or {}
            return fm, "".join(lines[i + 1:])
    return None, text


def join_frontmatter(fm: dict, body: str) -> str:
    return "---\n" + dump_yaml(fm) + "---\n" + body


# ---------------------------------------------------------------- ID

def kind_of(id_: str) -> str:
    m = ID_RE.match(str(id_))
    if not m:
        raise OrcError(f"Invalid ID: {id_}")
    return KIND_OF_PREFIX[m.group(1)]


def is_id(value) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value))


def content_path(root: Path, id_: str) -> Path:
    return orc(root) / CONTENT_DIR[kind_of(id_)] / f"{id_}.md"


def session_ids(root: Path) -> list:
    d = orc(root) / "sessions"
    return sorted(p.stem for p in d.glob("S-*.md")) if d.is_dir() else []


def next_id(root: Path, kind: str) -> str:
    prefix = PREFIX[kind]
    if kind == "session":
        existing = session_ids(root)
    else:
        existing = [str(e.get("id")) for e in load_registry(root, kind) if isinstance(e, dict)]
    nums = [int(m.group(2)) for m in map(ID_RE.match, existing) if m and m.group(1) == prefix]
    return f"{prefix}-{(max(nums) if nums else 0) + 1:04d}"


def find_entry(root: Path, id_: str):
    kind = kind_of(id_)
    if kind == "session":
        raise OrcError("Sessions are not in the registry; use the file sessions/S-x.md")
    items = load_registry(root, kind)
    for e in items:
        if isinstance(e, dict) and e.get("id") == id_:
            return kind, items, e
    raise OrcError(f"No such entry: {id_}")


def exists(root: Path, id_: str) -> bool:
    try:
        if kind_of(id_) == "session":
            return content_path(root, id_).exists()
        find_entry(root, id_)
        return True
    except OrcError:
        return False


def as_list(value) -> list:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def check_summary(summary) -> None:
    if summary is not None and len(str(summary)) > SUMMARY_MAX:
        raise OrcError(f"summary is {len(str(summary))} characters long (> {SUMMARY_MAX})")


def render_template(name: str, **values) -> str:
    text = read_text(TEMPLATES / name)
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


# ---------------------------------------------------------------- init

def cmd_init(root: Path, project_id: str, goal: str) -> list:
    o = orc(root)
    if (o / "config.yaml").exists():
        raise OrcError(f"{o} already has config.yaml; not overwriting.")
    kit_version = read_text(KIT_DIR / "VERSION").strip()
    created = []
    for d in ("state", "decisions", "artifacts", "contexts", "evidence", "sessions", "knowledge"):
        (o / d).mkdir(parents=True, exist_ok=True)
    write_text(o / "config.yaml", dump_yaml({
        "project_id": project_id, "kit_version": kit_version, "goal": goal}))
    created.append(o / "config.yaml")
    for kind in KINDS:
        save_registry(root, kind, [])
        created.append(registry_path(root, kind))
    write_text(o / ".gitignore",
               "# Hook session markers and graph cache, not committed\nruntime/\ncache/\n")
    created.append(o / ".gitignore")
    km = o / "knowledge" / "KNOWLEDGE_MAP.md"
    if not km.exists():
        write_text(km, render_template("KNOWLEDGE_MAP.md", project_id=project_id, date=today()))
        created.append(km)
    for d in ("decisions", "artifacts", "contexts", "evidence", "sessions"):
        keep = o / d / ".gitkeep"
        if not keep.exists():
            write_text(keep, "")
    claude_md = root / "CLAUDE.md"
    if not claude_md.exists():
        write_text(claude_md, render_template("CLAUDE.md", project_id=project_id, goal=goal))
        created.append(claude_md)
    return created


# ---------------------------------------------------------------- new

def make_entry(entity: str, /, **values) -> dict:
    entry = {k: (list(v) if isinstance(v, list) else v) for k, v in FIELDS[entity].items()}
    for k, v in values.items():
        if v is None:
            continue
        if k not in entry:
            raise OrcError(f"{entity} has no field '{k}'")
        entry[k] = as_list(v) if k in LIST_FIELDS[entity] else v
    check_summary(entry.get("summary"))
    return entry


def add_entry(root: Path, kind: str, entry: dict) -> dict:
    items = load_registry(root, kind)
    entry["id"] = next_id(root, kind)
    errs = entry_errors(root, kind, entry, extra_ids={entry["id"]})
    if errs:
        raise OrcError("; ".join(errs))
    items.append(entry)
    save_registry(root, kind, items)
    return entry


def new_task(root: Path, **values) -> dict:
    return add_entry(root, "task", make_entry("task", **values))


def new_artifact(root: Path, type_: str, task: str, summary: str = "",
                 supersedes: str | None = None, depends_on=None) -> dict:
    if type_ not in ART_TYPES:
        raise OrcError(f"type must be one of {ART_TYPES}")
    version = 1
    if supersedes:
        _, _, old = find_entry(root, supersedes)
        if old.get("type") != type_:
            raise OrcError(f"{supersedes} is a {old.get('type')}, not a {type_}")
        version = int(old.get("version") or 1) + 1
    entry = make_entry("artifact", type=type_, task=task, summary=summary,
                       supersedes=supersedes, depends_on=depends_on, version=version)
    entry = add_entry(root, "artifact", entry)
    rel = f"{ORC_DIR}/artifacts/{entry['id']}.md"
    entry["paths"] = [rel]
    _replace_entry(root, "artifact", entry)
    body = render_template(f"artifact-{type_}.md", id=entry["id"], task=task or "")
    write_text(content_path(root, entry["id"]), join_frontmatter(dict(entry), body))
    return entry


def new_decision(root: Path, summary: str, scope: str = "project",
                 supersedes: str | None = None, status: str = "proposed") -> dict:
    entry = add_entry(root, "decision", make_entry(
        "decision", summary=summary, scope=scope, supersedes=supersedes, status=status))
    write_text(content_path(root, entry["id"]),
               render_template("decision.md", id=entry["id"], summary=summary,
                               scope=scope, date=today()))
    if status == "accepted" and supersedes:
        set_fields(root, supersedes, {"status": "superseded"})
    return entry


def new_context(root: Path, task: str, summary: str, paths=None) -> dict:
    entry = add_entry(root, "context", make_entry("context", task=task, summary=summary,
                                                  paths=paths))
    write_text(content_path(root, entry["id"]),
               render_template("context.md", id=entry["id"], task=task or ""))
    return entry


def new_evidence(root: Path, task: str, summary: str, kind: str = "test",
                 result: str = "info", paths=None, with_file: bool = True, by: str = "agent") -> dict:
    entry = make_entry("evidence", task=task, summary=summary, kind=kind, result=result,
                       paths=paths, by=by)
    entry = add_entry(root, "evidence", entry)
    if with_file:
        rel = f"{ORC_DIR}/evidence/{entry['id']}.md"
        entry["paths"] = as_list(entry["paths"]) + [rel]
        _replace_entry(root, "evidence", entry)
        write_text(content_path(root, entry["id"]),
                   render_template("evidence.md", id=entry["id"], task=task or ""))
    return entry


def tasks_from_plan(root: Path, art_id: str) -> list:
    """Create tasks from the ```yaml orc-tasks block of an accepted plan."""
    _, _, art = find_entry(root, art_id)
    if art.get("type") != "plan":
        raise OrcError(f"{art_id} is not a plan")
    if art.get("status") != "accepted":
        raise OrcError(f"{art_id} is not accepted yet; the user approves it, then `orc advance {art_id} --to accepted`")
    _, body = split_frontmatter(read_text(content_path(root, art_id)))
    m = re.search(r"```ya?ml orc-tasks\s*\n(.*?)```", body, re.S)
    if not m:
        raise OrcError(f"{art_id} has no ```yaml orc-tasks block")
    specs = _yaml().safe_load(m.group(1)) or []
    if not isinstance(specs, list):
        raise OrcError("orc-tasks must be a list")
    plan_task = find_entry(root, art["task"])[2] if art.get("task") else {}
    plan_parent = plan_task.get("parent") or (plan_task["id"] if plan_task.get("workflow") == "k" else None)
    if load_config(root).get("workflow") == "k" and not plan_parent:
        raise OrcError(f"workflow k project: the plan tasks of {art_id} do not belong to a root task (`--parent`)")
    local = {}
    created = []
    for spec in specs:
        if not isinstance(spec, dict) or "key" not in spec or "title" not in spec:
            raise OrcError("Every task in orc-tasks needs 'key' and 'title'")
        blocked = [local.get(b, b) for b in as_list(spec.get("blocked_by"))]
        acceptance = [a if "#" in str(a) else f"{art_id}#{a}" for a in as_list(spec.get("acceptance"))]
        entry = new_task(root, title=spec["title"], owner=spec.get("owner", "coder"), parent=plan_parent,
                         summary=spec.get("summary", ""), blocked_by=blocked,
                         acceptance=acceptance, write_scope=spec.get("write_scope"),
                         inputs=[art_id] + as_list(spec.get("inputs")),
                         context=spec.get("context"))
        local[spec["key"]] = entry["id"]
        created.append(entry)
    return created


# ---------------------------------------------------------------- set

def _replace_entry(root: Path, kind: str, entry: dict) -> None:
    items = load_registry(root, kind)
    for i, e in enumerate(items):
        if isinstance(e, dict) and e.get("id") == entry["id"]:
            items[i] = entry
            save_registry(root, kind, items)
            return
    raise OrcError(f"No such entry: {entry['id']}")


def parse_assignments(pairs) -> dict:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise OrcError(f"Expected key=value: {pair}")
        k, v = pair.split("=", 1)
        out[k.strip()] = _yaml().safe_load(v) if v.strip() else None
    return out


def guarded_set(root: Path, id_: str, changes: dict) -> dict:
    """`orc set` for the LLM: never changes state fields."""
    bad = sorted(k for k in changes if k in STATE_FIELDS or k.startswith("approved_"))
    if bad and kind_of(id_) != "session":
        raise OrcError(f"`orc set` cannot change {bad}; state changes only through "
                       f"`orc advance {id_} --to <status>`, `orc apply` or `orc approve`")
    if kind_of(id_) == "decision":  # the content of an effective DEC is the user's authority
        _, _, d = find_entry(root, id_)
        locked = sorted(k for k in changes if k in ("summary", "scope", "supersedes"))
        if d.get("status") != "proposed" and locked:
            raise OrcError(f"{id_} is {d.get('status')}: {locked} cannot be edited; create a new DEC `--supersedes {id_}` "
                           "and let the user approve it")
    return set_fields(root, id_, changes)


def advance(root: Path, id_: str, to: str) -> dict:
    """Manual state transition per the ADVANCE_MOVES table (basic workflow rules)."""
    kind = kind_of(id_)
    if kind not in ADVANCE_MOVES:
        raise OrcError(f"{id_}: a {kind} has no status to advance")
    _, _, e = find_entry(root, id_)
    frm = e.get("status")
    if kind == "task" and e.get("workflow") == "k":
        import orc_k  # noqa: PLC0415
        entry, note = orc_k.advance_root(root, e, to)
        entry["_note"] = note
        return entry
    if kind == "task" and e.get("parent") and frm in ("blocked", "failed"):
        raise OrcError(f"{id_}: child task of a K request — the user unblocks it with `orc:resume {e['parent']}` "
                       f"then `orc resume {e['parent']}`")
    if kind == "artifact" and e.get("type") == "research" and to == "accepted":
        raise OrcError(f"{id_}: a research ART that records the user's answers is accepted only through "
                       f"`orc approval request {id_} ...` + the user typing `orc:approve ...` + `orc approve`")
    if (frm, to) not in ADVANCE_MOVES[kind]:
        allowed = sorted(t for f, t in ADVANCE_MOVES[kind] if f == frm)
        raise OrcError(f"{id_}: cannot move {frm} → {to}"
                       + (f" (allowed: {', '.join(allowed)})" if allowed else " (no manual transition)"))
    changes = {"status": to}
    if kind == "task":
        if to == "dispatched":
            status = {t["id"]: t.get("status") for t in load_registry(root, "task")}
            waiting = [b for b in as_list(e.get("blocked_by")) if status.get(b) != "done"]
            if waiting:
                raise OrcError(f"{id_}: still waiting for {', '.join(waiting)}")
            if frm == "done" and agent_for(e.get("owner")) == "coder":
                raise OrcError(f"{id_}: a done coder task cannot be dispatched again; create a new task")
        if frm in ("blocked", "failed"):
            if frm == "blocked" and not e.get("needs_user"):
                raise OrcError(f"{id_}: waiting on blocked_by, not on the user; use `orc next`")
            changes.update(needs_user=False, resume_status=None)
        if to == "dispatched" and agent_for(e.get("owner")) == "coder":
            import orc_k  # noqa: PLC0415
            orc_k.child_dispatch_check(root, e)  # K request: root IMPLEMENTING + a valid approval
            import orc_verify  # noqa: PLC0415 - same cli/ directory
            import orc_approval  # noqa: PLC0415
            ok, why = orc_approval.check(root, id_)
            if not ok:  # shadow: record only, do not block
                orc_approval.shadow_log(root, {"event": "approval_missing", "task": id_, "reason": why})
            cp = orc_verify.make_checkpoint(root, id_)  # None: verify not configured, or a checkpoint exists
            if cp:
                with journal_context(extra={"checkpoint": cp}):
                    return set_fields(root, id_, changes)
    return set_fields(root, id_, changes)


def set_fields(root: Path, id_: str, changes: dict) -> dict:
    """Write fields without checking transition rules — internal use; the CLI uses guarded_set/advance."""
    if "id" in changes:
        raise OrcError("The id cannot be changed")
    kind = kind_of(id_)
    if kind == "session":
        path = content_path(root, id_)
        if not path.exists():
            raise OrcError(f"No such entry: {id_}")
        fm, body = split_frontmatter(read_text(path))
        fm = fm or {}
        for k, v in changes.items():
            if k not in SESSION_FIELDS and k != "request":
                raise OrcError(f"session has no field '{k}'")
            fm[k] = v
        write_text(path, join_frontmatter(fm, body))
        return fm
    _, _, entry = find_entry(root, id_)
    for k, v in changes.items():
        if k not in FIELDS[kind]:
            raise OrcError(f"{kind} has no field '{k}'")
        entry[k] = as_list(v) if k in LIST_FIELDS[kind] else v
    errs = entry_errors(root, kind, entry)
    if errs:
        raise OrcError("; ".join(errs))
    _replace_entry(root, kind, entry)
    if kind == "artifact":
        path = content_path(root, id_)
        if path.exists():
            fm, body = split_frontmatter(read_text(path))
            fm = fm or {}
            for k in FIELDS["artifact"]:
                fm[k] = entry[k]
            write_text(path, join_frontmatter(fm, body))
    if kind == "decision" and changes.get("status") == "accepted" and entry.get("supersedes"):
        set_fields(root, entry["supersedes"], {"status": "superseded"})
    return entry


# ---------------------------------------------------------------- validate

def entry_errors(root: Path, kind: str, e: dict, extra_ids=frozenset()) -> list:
    """Errors of one registry entry (schema + references)."""
    errs = []
    eid = e.get("id")
    label = eid or f"<{kind}>"
    if not is_id(eid) or kind_of(eid) != kind:
        errs.append(f"{label}: malformed id for {kind}")
    missing = [f for f in FIELDS[kind] if f not in e and f not in OPTIONAL_FIELDS.get(kind, ())]
    if missing:
        errs.append(f"{label}: missing fields {missing}")
    unknown = [f for f in e if f not in FIELDS[kind]]
    if unknown:
        errs.append(f"{label}: unknown fields {unknown}")
    if e.get("summary") is not None and len(str(e.get("summary"))) > SUMMARY_MAX:
        errs.append(f"{label}: summary > {SUMMARY_MAX} characters")
    for f in LIST_FIELDS[kind]:
        if f in e and not isinstance(e[f], list):
            errs.append(f"{label}: {f} must be a list")

    def ref(value, field, want=None):
        if value in (None, ""):
            return
        if not is_id(value):
            errs.append(f"{label}: {field} = {value!r} is not an ID")
            return
        if want and kind_of(value) != want:
            errs.append(f"{label}: {field} = {value} must be a {want}")
            return
        if value not in extra_ids and not exists(root, value):
            errs.append(f"{label}: {field} references {value}, which does not exist")

    if kind == "task":
        import orc_k  # noqa: PLC0415
        allowed = orc_k.K_STATES if e.get("workflow") == "k" else TASK_STATUS
        if e.get("status") not in allowed:
            errs.append(f"{label}: invalid status '{e.get('status')}'")
        if e.get("workflow") not in (None, "k"):
            errs.append(f"{label}: workflow must be k or empty")
        if e.get("recovery_action") not in (None, *orc_k.RECOVERY):
            errs.append(f"{label}: invalid recovery_action")
        if e.get("parent"):
            ref(e.get("parent"), "parent", "task")
            if e.get("parent") not in extra_ids and exists(root, e.get("parent")):
                if find_entry(root, e["parent"])[2].get("workflow") != "k":
                    errs.append(f"{label}: parent {e['parent']} is not a K root task")
        if not OWNER_RE.match(str(e.get("owner"))):
            errs.append(f"{label}: invalid owner '{e.get('owner')}'")
        if not str(e.get("title") or "").strip():
            errs.append(f"{label}: missing title")
        if e.get("failure_class") not in (None, *FAILURE_CLASSES):
            errs.append(f"{label}: invalid failure_class '{e.get('failure_class')}'")
        if not isinstance(e.get("attempts"), int) or e.get("attempts") < 0:
            errs.append(f"{label}: attempts must be an integer ≥ 0")
        if e.get("resume_status") not in (None, *allowed):
            errs.append(f"{label}: invalid resume_status")
        if e.get("level") not in (None, 0, 1, 2, 3):
            errs.append(f"{label}: level must be 0..3")
        for b in as_list(e.get("blocked_by")):
            ref(b, "blocked_by", "task")
            if b == eid:
                errs.append(f"{label}: blocks itself")
        for c in as_list(e.get("context")):
            ref(c, "context", "context")
        for f in ("inputs", "outputs"):
            for v in as_list(e.get(f)):
                if is_id(v):
                    ref(v, f)
        for a in as_list(e.get("acceptance")):
            m = ACC_RE.match(str(a))
            if not m:
                errs.append(f"{label}: acceptance '{a}' must look like ART-x#AC-n")
                continue
            ref(m.group(1), "acceptance", "artifact")
            if exists(root, m.group(1)) and acceptance_text(root, str(a)) is None:
                errs.append(f"{label}: {a} is not in {m.group(1)}")
    elif kind == "artifact":
        if e.get("type") not in ART_TYPES:
            errs.append(f"{label}: invalid type '{e.get('type')}'")
        if e.get("status") not in ART_STATUS:
            errs.append(f"{label}: invalid status '{e.get('status')}'")
        if not isinstance(e.get("version"), int) or e.get("version") < 1:
            errs.append(f"{label}: version must be an integer ≥ 1")
        ref(e.get("task"), "task", "task")
        ref(e.get("supersedes"), "supersedes", "artifact")
        for d in as_list(e.get("depends_on")):
            ref(d, "depends_on")
    elif kind == "decision":
        if e.get("status") not in DEC_STATUS:
            errs.append(f"{label}: invalid status '{e.get('status')}'")
        ref(e.get("supersedes"), "supersedes", "decision")
        if not str(e.get("scope") or "").strip():
            errs.append(f"{label}: missing scope")
    elif kind == "context":
        ref(e.get("task"), "task", "task")
        if e.get("source") not in CTX_SOURCES:
            errs.append(f"{label}: invalid source '{e.get('source')}'")
        if e.get("graph_state") not in GRAPH_STATES:
            errs.append(f"{label}: invalid graph_state '{e.get('graph_state')}'")
        if not isinstance(e.get("truncated"), bool):
            errs.append(f"{label}: truncated must be true/false")
        fp = e.get("fingerprint")
        if fp is not None and not (isinstance(fp, dict) and isinstance(fp.get("files"), dict)):
            errs.append(f"{label}: fingerprint must look like {{graph, files}}")
    elif kind == "evidence":
        ref(e.get("task"), "task", "task")
        if e.get("kind") not in EVD_KINDS:
            errs.append(f"{label}: invalid kind '{e.get('kind')}'")
        if e.get("result") not in EVD_RESULTS:
            errs.append(f"{label}: invalid result '{e.get('result')}'")
        if e.get("by", "agent") not in EVD_BY:
            errs.append(f"{label}: invalid by '{e.get('by')}'")
    return errs


def acceptance_text(root: Path, ref: str):
    m = ACC_RE.match(ref)
    if not m:
        return None
    path = content_path(root, m.group(1))
    if not path.exists():
        return None
    _, body = split_frontmatter(read_text(path))
    pat = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?" + re.escape(m.group(2)) + r"\b.*$", re.M)
    hit = pat.search(body)
    return hit.group(0).strip() if hit else None


def session_errors(root: Path, path: Path, require_complete: bool) -> list:
    errs = []
    label = path.stem
    try:
        fm, body = split_frontmatter(read_text(path))
    except Exception as exc:  # broken YAML
        return [f"{label}: frontmatter YAML error ({exc})"]
    if fm is None:
        return [f"{label}: missing frontmatter"]
    for f in SESSION_FIELDS:
        if f not in fm:
            errs.append(f"{label}: missing field {f}")
    if fm.get("id") != label:
        errs.append(f"{label}: frontmatter id ({fm.get('id')}) differs from the file name")
    task = fm.get("task")
    if task not in (None, "", "-"):
        if not is_id(task) or kind_of(task) != "task" or not exists(root, task):
            errs.append(f"{label}: task {task} does not exist")
    result = fm.get("result")
    allowed = HANDOFF_RESULTS if require_complete else (*HANDOFF_RESULTS, "pending")
    if result not in allowed:
        errs.append(f"{label}: result '{result}' must be {'|'.join(allowed)}")
    if require_complete:
        for sec in HANDOFF_SECTIONS:
            content = section_body(body, sec)
            if content is None:
                errs.append(f"{label}: missing section '## {sec}'")
            elif not content or content == PLACEHOLDER:
                errs.append(f"{label}: section '{sec}' not filled in (write 'None' if empty)")
    return errs


def section_body(body: str, title: str):
    m = re.search(r"^##\s+" + re.escape(title) + r"\s*$(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    return m.group(1).strip() if m else None


def validate(root: Path) -> list:
    errs = []
    cfg = load_config(root)
    for f in ("project_id", "kit_version"):
        if not cfg.get(f):
            errs.append(f"config.yaml: missing {f}")
    for kind in KINDS:
        try:
            items = load_registry(root, kind)
        except Exception as exc:
            errs.append(f"{REGISTRY_FILE[kind]}.yaml: {exc}")
            continue
        seen = set()
        for e in items:
            if not isinstance(e, dict):
                errs.append(f"{REGISTRY_FILE[kind]}.yaml: entry is not a mapping: {e!r}")
                continue
            if e.get("id") in seen:
                errs.append(f"{e.get('id')}: duplicate ID")
            seen.add(e.get("id"))
            errs.extend(entry_errors(root, kind, e))
            if kind in ("artifact", "decision") and is_id(e.get("id")):
                path = content_path(root, e["id"])
                if not path.exists():
                    errs.append(f"{e['id']}: missing file {path.relative_to(root).as_posix()}")
                elif kind == "artifact":
                    errs.extend(_artifact_file_errors(path, e))
        # Content files not in the registry
        if kind in CONTENT_DIR:
            d = orc(root) / CONTENT_DIR[kind]
            for p in d.glob(f"{PREFIX[kind]}-*.md") if d.is_dir() else []:
                if p.stem not in seen:
                    errs.append(f"{p.stem}: file exists but is not in the registry")
    for p in (orc(root) / "sessions").glob("S-*.md"):
        errs.extend(session_errors(root, p, require_complete=False))
    errs.extend(journal_errors(root))
    return errs


def _artifact_file_errors(path: Path, entry: dict) -> list:
    try:
        fm, _ = split_frontmatter(read_text(path))
    except Exception as exc:
        return [f"{entry['id']}: frontmatter YAML error ({exc})"]
    if fm is None:
        return [f"{entry['id']}: missing frontmatter in the file"]
    errs = []
    for k in FIELDS["artifact"]:
        if k not in fm:
            errs.append(f"{entry['id']}: frontmatter is missing {k}")
        elif fm[k] != entry.get(k):
            errs.append(f"{entry['id']}: frontmatter {k}={fm[k]!r} differs from the registry {entry.get(k)!r}")
    return errs


# ---------------------------------------------------------------- sessions & handoff

def marker_path(root: Path, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return orc(root) / "runtime" / "sessions" / f"{safe}.yaml"


def handoff_open(root: Path, role: str, task: str | None = None, key: str | None = None,
                 request: str = "") -> str:
    if task not in (None, "", "-") and not exists(root, task):
        raise OrcError(f"No such entry: {task}")
    if key:
        m = load_yaml_file(marker_path(root, key))
        if m and not m.get("closed"):
            raise OrcError(f"This session already opened {m.get('sid')}; one request per session")
    sid = next_id(root, "session")
    fm = {"id": sid, "task": task if task not in (None, "", "-") else None, "role": role,
          "result": "pending", "request": request, "date": today()}
    body = render_template("session.md", placeholder=PLACEHOLDER)
    write_text(content_path(root, sid), join_frontmatter(fm, body))
    if key:
        write_text(marker_path(root, key), dump_yaml(
            {"sid": sid, "opened": _dt.datetime.now().isoformat(timespec="seconds"),
             "blocks": 0, "closed": False}))
    return sid


def open_marker(root: Path, key: str | None):
    if not key:
        return None
    m = load_yaml_file(marker_path(root, key))
    return m if m and not m.get("closed") else None


def handoff_errors(root: Path, sid: str) -> list:
    path = content_path(root, sid)
    if not path.exists():
        return [f"{sid}: no handoff file yet {path.relative_to(root).as_posix()}"]
    return session_errors(root, path, require_complete=True)


def close_marker(root: Path, key: str, how: str = "handoff") -> None:
    path = marker_path(root, key)
    m = load_yaml_file(path) or {}
    m.update({"closed": True, "closed_by": how,
              "closed_at": _dt.datetime.now().isoformat(timespec="seconds")})
    write_text(path, dump_yaml(m))


def stop_check(root: Path, key: str | None) -> dict | None:
    """Stop hook logic. Returns the blocking JSON, or None to allow stopping."""
    m = open_marker(root, key)
    if not m:
        return None  # the session opened no task/S-ID → do not block
    errs = handoff_errors(root, m["sid"]) + validate(root)
    if not errs:
        close_marker(root, key)
        return None
    if int(m.get("blocks", 0)) >= MAX_STOP_BLOCKS:
        close_marker(root, key, how="gave_up")
        return None
    m["blocks"] = int(m.get("blocks", 0)) + 1
    write_text(marker_path(root, key), dump_yaml(m))
    rel = content_path(root, m["sid"]).relative_to(root).as_posix()
    reason = (f"[orchestra] Cannot finish yet: handoff {m['sid']} ({rel}) is not valid "
              f"(block {m['blocks']}/{MAX_STOP_BLOCKS}). Fill in all 5 sections, set result "
              f"done|partial|blocked, then run `orc validate --handoff`.\n- "
              + "\n- ".join(errs[:15]))
    return {"decision": "block", "reason": reason}


# ---------------------------------------------------------------- next & route

def promote(root: Path) -> list:
    """pending/blocked whose blocked_by are all done → ready (or resume_status)."""
    items = load_registry(root, "task")
    status = {t["id"]: t.get("status") for t in items}
    changed = []
    for t in items:
        blockers = as_list(t.get("blocked_by"))
        clear = all(status.get(b) == "done" for b in blockers)
        if t["status"] == "pending" and clear:
            t["status"] = "ready"
            changed.append(t["id"])
        elif t["status"] == "blocked" and blockers and clear and not t.get("needs_user"):
            t["status"] = t.get("resume_status") or "ready"
            t["resume_status"] = None
            changed.append(t["id"])
    if changed:
        save_registry(root, "task", items)
    return changed


def agent_for(owner: str) -> str:
    return "coder" if str(owner).startswith("coder") else str(owner)


def next_report(root: Path) -> str:
    promoted = promote(root)
    write_active(root)
    tasks = load_registry(root, "task")
    import orc_k  # noqa: PLC0415
    lines = orc_k.next_lines(root, tasks)
    if promoted:
        lines.append("Moved to ready/resume: " + ", ".join(promoted))
    ask = [t for t in tasks if t.get("needs_user")]
    for t in ask:
        lines.append(f"ASK USER: {t['id']} [{t['status']}] {t.get('failure_class') or ''} — {t['title']}")
    review = [t for t in tasks if t["status"] == "in_review"]
    for t in review:
        lines.append(f"REVIEW: {t['id']} → /orchestra:review {t['id']} (agent orchestra:reviewer)")
    ready = [t for t in tasks if t["status"] == "ready"]
    for t in ready:
        lines.append(f"READY: {t['id']} owner={t['owner']} → agent orchestra:{agent_for(t['owner'])} — {t['title']}")
    if ready:
        lines.append(f"NEXT: {ready[0]['id']}")
    elif not review and not ask:
        lines.append("NEXT: (no ready task)")
    return "\n".join(lines)


ENVELOPE_KEYS = {"msg", "task_id", "status", "outputs", "evidence",
                 "proposed_decisions", "summary", "failure_class"}


def parse_envelope(text: str) -> dict:
    text = text.strip()
    fence = re.match(r"^```(?:ya?ml)?\s*\n(.*?)\n```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        env = _yaml().safe_load(text)
    except Exception as exc:
        raise OrcError(f"Envelope is not valid YAML: {exc}") from exc
    if not isinstance(env, dict):
        raise OrcError("The envelope must be a YAML mapping (no prose)")
    extra = set(env) - ENVELOPE_KEYS
    if extra:
        raise OrcError(f"Envelope has unknown keys: {sorted(extra)}")
    if env.get("msg") != "result":
        raise OrcError("The envelope needs msg: result")
    for k in ("task_id", "status", "summary"):
        if not env.get(k):
            raise OrcError(f"Envelope is missing {k}")
    if env["status"] not in ENV_STATUS:
        raise OrcError(f"status must be {'|'.join(ENV_STATUS)}")
    for k in ("outputs", "evidence", "proposed_decisions"):
        env[k] = as_list(env.get(k))
    check_summary(env["summary"])
    if env.get("failure_class") not in (None, *FAILURE_CLASSES):
        raise OrcError(f"failure_class must be {'|'.join(FAILURE_CLASSES)}")
    return env


def _in_scope(path: str, scope: list) -> bool:
    """write_scope matches gitignore-style, anchored at the project root: `*` does not cross `/`."""
    from orc_guard import match_path  # noqa: PLC0415 - same cli/ directory, stdlib only
    return match_path(path, [str(s) for s in scope])


def apply_envelope(root: Path, env: dict) -> str:
    """Write a subagent's result into state and route deterministically. Returns the ROUTE line."""
    _, _, task = find_entry(root, env["task_id"])
    tid = task["id"]
    phase = task["status"]
    if phase not in ("dispatched", "in_review"):
        raise OrcError(f"{tid} is {phase}; envelopes are accepted only when dispatched or in_review")
    reviewing = phase == "in_review"
    if env.get("failure_class") and not reviewing:
        raise OrcError("failure_class is only for review results")
    if reviewing and env["status"] != "done" and not env.get("failure_class"):
        raise OrcError("A failed review must have a failure_class")

    # Check everything before writing anything
    outputs = []
    for o in env["outputs"]:
        o = str(o)
        if is_id(o):
            if not exists(root, o):
                raise OrcError(f"outputs: {o} does not exist")
        elif (not reviewing and agent_for(task["owner"]) == "coder"
              and not _in_scope(o, as_list(task.get("write_scope")))):
            raise OrcError(f"outputs: {o} is outside write_scope {task.get('write_scope')}")
        outputs.append(o)
    for ev in env["evidence"]:
        if isinstance(ev, str):
            if not (is_id(ev) and kind_of(ev) == "evidence" and exists(root, ev)):
                raise OrcError(f"evidence: {ev} does not exist")
        elif isinstance(ev, dict):
            unknown = set(ev) - {"kind", "result", "summary", "paths"}
            if unknown or not ev.get("summary"):
                raise OrcError("new evidence needs a summary; only kind/result/summary/paths are allowed")
            if ev.get("kind") in ORC_ONLY_KINDS:
                raise OrcError(f"evidence kind '{ev['kind']}' is created only by `orc verify`")
            check_summary(ev["summary"])
        else:
            raise OrcError("evidence must be an EVD ID or a mapping")
    for d in env["proposed_decisions"]:
        if isinstance(d, dict):
            if not d.get("summary"):
                raise OrcError("proposed_decisions need a summary")
            check_summary(d["summary"])
        elif not isinstance(d, str):
            raise OrcError("proposed_decisions must be strings or mappings")

    # Coder task of a K request: route the review through the verify gate (computed before writing; no verify ⇒ refuse)
    k_route, k_parent = None, None
    if task.get("parent"):
        import orc_k  # noqa: PLC0415
        k_parent = find_entry(root, task["parent"])[2]
        if not orc_k.is_root(k_parent):
            k_parent = None
        elif reviewing and agent_for(task["owner"]) == "coder":
            k_route = orc_k.review_route(root, task, env)
            if k_route[0] is None:  # SPEC_GAP at L0/L1: replan as in the basic workflow
                k_route = None

    # Write
    evd_ids = []
    for ev in env["evidence"]:
        if isinstance(ev, str):
            evd_ids.append(ev)
        else:
            e = new_evidence(root, tid, ev["summary"], kind=ev.get("kind", "test"),
                             result=ev.get("result", "info"), paths=ev.get("paths"),
                             with_file=False)
            evd_ids.append(e["id"])
    dec_ids = []
    for d in env["proposed_decisions"]:
        if isinstance(d, str):
            d = {"summary": d}
        dec_ids.append(new_decision(root, d["summary"], scope=d.get("scope", tid),
                                    supersedes=d.get("supersedes"))["id"])

    _, _, task = find_entry(root, tid)
    for o in outputs + evd_ids:
        if o not in task["outputs"]:
            task["outputs"].append(o)
    task["summary"] = env["summary"]
    status, fc = env["status"], env.get("failure_class")
    route = ""
    if not reviewing:
        if status == "done":
            if agent_for(task["owner"]) == "coder":
                task["status"], route = "in_review", f"/orchestra:review {tid}"
            else:
                task["status"], route = "done", "orc next"
        elif status == "partial":
            task["status"], route = "ready", f"run {tid} again (partial)"
        elif status == "blocked":
            task.update(status="blocked", needs_user=True)
            route = f"ASK USER: {tid} is blocked"
        else:
            task.update(status="failed", needs_user=True)
            route = f"ASK USER: {tid} failed"
    elif k_route:
        task.update(k_route[0])
        route = k_route[1]
    elif status == "done" and not fc:
        task.update(status="done", failure_class=None)
        route = "orc next"
    else:
        task["failure_class"] = fc
        if fc == "IMPL_BUG":
            task["attempts"] = int(task.get("attempts") or 0) + 1
            if task["attempts"] > MAX_ATTEMPTS:
                task.update(status="blocked", needs_user=True)
                route = f"ASK USER: {tid} tried {task['attempts']} times"
            else:
                task["status"] = "ready"
                route = f"send {tid} back to coder (attempt {task['attempts']})"
        elif fc == "SPEC_GAP":
            plans = [a.split("#")[0] for a in task["acceptance"]]
            fix = new_task(root, title=f"Replan {tid}", owner="researcher", parent=task.get("parent"),
                           inputs=[tid] + sorted(set(plans)),
                           summary=f"SPEC_GAP from review of {tid}; the new plan must supersede the old one")
            task.update(status="blocked", resume_status="ready",
                        blocked_by=task["blocked_by"] + [fix["id"]])
            route = f"{fix['id']} → researcher replan (supersedes)"
        elif fc == "DECISION_CONFLICT":
            task.update(status="blocked", needs_user=True)
            route = f"ASK USER: {tid} decision conflict"
        elif fc == "REGRESSION":
            fix = new_task(root, title=f"Fix the regression of {tid}", owner="coder", parent=task.get("parent"),
                           write_scope=task["write_scope"], inputs=[tid],
                           acceptance=task["acceptance"],
                           summary=f"REGRESSION found while reviewing {tid}; merge blocked")
            task.update(status="blocked", resume_status="in_review",
                        blocked_by=task["blocked_by"] + [fix["id"]])
            route = f"BLOCK MERGE {tid}; {fix['id']} → coder"
    with journal_context(evidence=evd_ids, extra={"recovery_action": task.get("recovery_action")}
                         if task.get("recovery_action") else None):
        _replace_entry(root, "task", task)
    if k_parent and task["status"] in ("blocked", "failed") and task.get("needs_user"):
        import orc_k  # noqa: PLC0415
        orc_k.escalate(root, task, route)  # ESCALATE on a child task ⇒ root task BLOCKED
        route += f"; {k_parent['id']} → BLOCKED"
    extra = []
    if evd_ids:
        extra.append("evidence " + ",".join(evd_ids))
    if dec_ids:
        extra.append("decisions awaiting approval " + ",".join(dec_ids))
    return f"ROUTE: {tid} → {task['status']}; {route}" + (f" ({'; '.join(extra)})" if extra else "")


# ---------------------------------------------------------------- show & brief

def show(root: Path, id_: str, meta: bool = False, pack: bool = False) -> str:
    kind = kind_of(id_)
    if kind == "session":
        path = content_path(root, id_)
        if not path.exists():
            raise OrcError(f"No such entry: {id_}")
        fm, body = split_frontmatter(read_text(path))
        return dump_yaml(fm) if meta else read_text(path)
    _, _, e = find_entry(root, id_)
    if pack:
        if kind != "task":
            raise OrcError("--pack is only for TASK")
        # Package for the reviewer: acceptance + outputs + evidence, without the coder's summary.
        lines = [f"task: {e['id']}", f"title: {e['title']}", "acceptance:"]
        for a in e["acceptance"]:
            lines.append(f"  - {a}: {acceptance_text(root, a) or '(not found)'}")
        lines.append("write_scope: " + json.dumps(e["write_scope"], ensure_ascii=False))
        lines.append("outputs:")
        for o in e["outputs"]:
            if is_id(o) and kind_of(o) == "evidence":
                continue
            lines.append(f"  - {o}")
        lines.append("evidence:")
        for o in e["outputs"]:
            if is_id(o) and kind_of(o) == "evidence":
                _, _, ev = find_entry(root, o)
                lines.append(f"  - {o}: kind={ev['kind']} result={ev['result']} by={ev.get('by', 'agent')} "
                             f"paths={ev['paths']}")
        return "\n".join(lines) + "\n"
    out = dump_yaml(e)
    if meta:
        return out
    if kind == "task":
        acc = [f"- {a}: {acceptance_text(root, a) or '(not found)'}" for a in e["acceptance"]]
        return out + ("\n# acceptance\n" + "\n".join(acc) + "\n" if acc else "")
    path = content_path(root, id_)
    return out + ("\n" + read_text(path) if path.exists() else "")


def _short(text, n=120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def latest_handoff(root: Path):
    best = None
    for sid in session_ids(root):
        fm, body = split_frontmatter(read_text(content_path(root, sid)))
        if fm and fm.get("result") in HANDOFF_RESULTS:
            best = (sid, fm, body)
    return best


def brief_text(root: Path, key: str | None = None, max_chars: int = BRIEF_MAX_CHARS) -> str:
    cfg = load_config(root)
    tasks = load_registry(root, "task")
    decisions = load_registry(root, "decision")
    out = [f"# Orchestra brief — {cfg.get('project_id')} (kit {cfg.get('kit_version')})",
           f"Goal: {_short(cfg.get('goal'), 300)}"]
    if cfg.get("workflow") == "k":
        out.append(f"Workflow: K (verify: {'yes' if cfg.get('verify') else 'MISSING — K cannot reach DONE'})")
    if safe_mode():
        out.append("⚠ ORC_SAFE_MODE is on: only the kit and .orchestra/config.yaml may be edited; orc refuses commands "
                   "that change the workflow. The user turns this variable off when the repair is done.")
    m = open_marker(root, key)
    if m:
        out.append(f"This session has {m['sid']} open (handoff required before stopping).")
    elif key:
        out.append(f"Open a session when a request creates or works on tasks: "
                   f"`orc handoff open --key {key} --role orchestrator --task <TASK-x|-> "
                   f"--request \"...\"`")
    ask = [t for t in tasks if t.get("needs_user")]
    if ask:
        out.append("\n## Waiting for the user")
        out += [f"- {t['id']} [{t['status']}] {t.get('failure_class') or ''} {_short(t['title'], 80)}"
                for t in ask]
    import orc_k  # noqa: PLC0415
    open_tasks = [t for t in tasks if not orc_k.is_closed(t.get("status"))]
    out.append(f"\n## Open tasks ({len(open_tasks)})")
    out += [f"- {t['id']} [{t['status']}] {t['owner']}: {_short(t['title'], 70)}"
            + (f" — {_short(t.get('summary'), 100)}" if t.get("summary") else "")
            for t in open_tasks[:12]] or ["- (none)"]
    ready = [t["id"] for t in open_tasks if t["status"] == "ready"]
    out.append("Ready: " + (", ".join(ready) if ready else "(none)"))
    h = latest_handoff(root)
    if h:
        sid, fm, body = h
        out.append(f"\n## Latest handoff: {sid} (task {fm.get('task')}, {fm.get('role')}, "
                   f"{fm.get('result')})")
        for sec in ("Next steps", "Open questions", "Decisions made"):
            text = section_body(body, sec)
            if text:
                out.append(f"{sec}: {_short(text, 400)}")
    accepted = [d for d in decisions if d.get("status") == "accepted"]
    proposed = [d for d in decisions if d.get("status") == "proposed"]
    if accepted:
        out.append("\n## Decisions in effect")
        out += [f"- {d['id']} ({d['scope']}): {_short(d['summary'], 160)}" for d in accepted[-12:]]
    if proposed:
        out.append("\n## Decisions awaiting the user's approval")
        out += [f"- {d['id']} ({d['scope']}): {_short(d['summary'], 160)}" for d in proposed[-8:]]
    graph_line = graph_brief_line(root)
    if graph_line:
        out.append(graph_line)
    out.append("\nCommands: `orc next`, `orc show <ID> --meta`, /orchestra:plan /orchestra:run /orchestra:review /orchestra:next /orchestra:handoff")
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[: max_chars - 40].rstrip() + "\n… (brief truncated)"
    return text


# ---------------------------------------------------------------- gate & report

ASK_SECTION = "Decisions for the user"
NOTHING_TO_ASK = {"none", "no", "n/a", "-"}


def plan_tasks_block(body: str):
    m = re.search(r"```ya?ml orc-tasks\s*\n(.*?)```", body, re.S)
    if not m:
        return None
    try:
        specs = _yaml().safe_load(m.group(1)) or []
    except Exception:
        return None
    return specs if isinstance(specs, list) else None


def gate(root: Path, art_id: str) -> list:
    """Can the plan proceed on its own? Returns the reasons to ask the user (empty = proceed)."""
    _, _, art = find_entry(root, art_id)
    if art.get("type") != "plan":
        raise OrcError(f"{art_id} is not a plan")
    _, body = split_frontmatter(read_text(content_path(root, art_id)))
    reasons = []
    asks = section_body(body, ASK_SECTION)
    if asks is None:
        reasons.append(f"the plan is missing the section '## {ASK_SECTION}'")
    else:
        lines = [l.strip() for l in re.sub(r"<!--.*?-->", "", asks, flags=re.S).splitlines()]
        lines = [l for l in lines if l and l.lstrip("-* ").strip().lower().rstrip(".") not in NOTHING_TO_ASK]
        reasons += [f"the plan asks: {l.lstrip('-* ').strip()}" for l in lines]
    specs = plan_tasks_block(body)
    if not specs:
        reasons.append("the plan has no valid orc-tasks block")
    elif any(not isinstance(t, dict) or str(t.get("title", "")).strip() in ("", "...")
             for t in specs):
        reasons.append("the plan still has template tasks (title '...')")
    if re.search(r"^\s*-\s*AC-1:\s*\.\.\.\s*$", body, re.M):
        reasons.append("the plan still has template criteria (AC-1: ...)")
    if not re.search(r"^\s*[-*]?\s*(?:\*\*)?AC-\d+\b", body, re.M):
        reasons.append("the plan has no acceptance criteria AC-n")
    scopes = {art_id, art.get("task")}
    for d in load_registry(root, "decision"):
        if d.get("status") == "proposed" and d.get("scope") in scopes:
            reasons.append(f"decision awaiting approval {d['id']}: {_short(d['summary'], 160)}")
    return reasons


UNDERSTANDING_SECTIONS = ("My understanding of the request", "Preliminary design",
                          "Unclear / missing information", "Questions for the user")


def ask_text(root: Path, art_id: str) -> str:
    """Extract, verbatim, the understanding + questions of a research artifact to pass to the user."""
    _, _, art = find_entry(root, art_id)
    if art.get("type") != "research":
        raise OrcError(f"{art_id} is not a research artifact")
    _, body = split_frontmatter(read_text(content_path(root, art_id)))
    out, errs = [f"# {art_id}: {art.get('summary') or 'Understanding & design'}"], []
    for sec in UNDERSTANDING_SECTIONS:
        text = section_body(body, sec)
        text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S).strip() if text is not None else None
        if text is None:
            errs.append(f"missing section '## {sec}'")
        elif not text or text == PLACEHOLDER:
            errs.append(f"section '{sec}' not filled in")
        else:
            out += ["", f"## {sec}", text]
    if errs:
        raise OrcError(f"{art_id}: " + "; ".join(errs))
    return "\n".join(out) + "\n"


def report(root: Path, task_id: str) -> str:
    """Deterministic report for one request: root task, plan, child tasks, review, evidence, decisions."""
    _, _, root_task = find_entry(root, task_id)
    arts = load_registry(root, "artifact")
    tasks = load_registry(root, "task")
    evidence = {e["id"]: e for e in load_registry(root, "evidence")}
    plans = [a for a in arts if a.get("type") == "plan" and a.get("task") == task_id]
    plan_ids = {a["id"] for a in plans}
    kids = [t for t in tasks if plan_ids & set(as_list(t.get("inputs"))) and t["id"] != task_id]
    # tasks created by routing (replan, regression fix) point their inputs at the child task
    kid_ids = {t["id"] for t in kids}
    kids += [t for t in tasks if t["id"] not in kid_ids and kid_ids & set(as_list(t.get("inputs")))]
    out = [f"# Report {task_id}: {root_task['title']}", ""]
    for a in plans:
        out.append(f"- Plan {a['id']} v{a['version']} [{a['status']}]: {a['summary']}")
    out += ["", "## Tasks", "| Task | Status | Attempts | Result |", "|---|---|---|---|"]
    for t in kids:
        flag = f" ⚠ {t['failure_class']}" if t.get("failure_class") else ""
        ask = " **needs user**" if t.get("needs_user") else ""
        out.append(f"| {t['id']} {_short(t['title'], 60)} | {t['status']}{flag}{ask} | "
                   f"{t.get('attempts', 0)} | {_short(t.get('summary'), 140)} |")
    out += ["", "## Evidence"]
    for t in kids:
        evs = [evidence[o] for o in as_list(t.get("outputs")) if o in evidence]
        if evs:
            res = ", ".join(f"{e['kind']}:{e['result']}" for e in evs)
            out.append(f"- {t['id']}: {res}")
        reviews = [a for a in arts if a.get("task") == t["id"] and a.get("type") == "review_report"]
        for r in reviews:
            out.append(f"  - review {r['id']} [{r['status']}] `{(r.get('paths') or [''])[0]}`")
        files = [o for o in as_list(t.get("outputs")) if not is_id(o)]
        if files:
            out.append("  - file: " + ", ".join(f"`{f}`" for f in files))
    scopes = {task_id} | plan_ids | {t["id"] for t in kids}
    decs = [d for d in load_registry(root, "decision") if d.get("scope") in scopes]
    if decs:
        out += ["", "## Decisions"]
        out += [f"- {d['id']} [{d['status']}]: {d['summary']}" for d in decs]
    ask = [t for t in kids if t.get("needs_user")]
    open_ = [t for t in kids if t["status"] not in ("done", "aborted")]
    out += ["", "## Conclusion",
            ("All tasks done." if not open_ else
             f"{len(open_)} tasks not done: " + ", ".join(f"{t['id']}[{t['status']}]" for t in open_))]
    if ask:
        out.append("Needs the user: " + ", ".join(t["id"] for t in ask))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- graph & context

def graph_adapter(root: Path):
    """Adapter for the `graph:` section of config.yaml. Imported lazily, engine-independent."""
    from graph_adapter import GraphAdapter  # noqa: PLC0415 - same cli/ directory
    return GraphAdapter(root, load_config(root).get("graph"))


def graph_brief_line(root: Path) -> str:
    """One graph status line for the brief. Never breaks the brief."""
    try:
        if not load_config(root).get("graph"):
            return ""
        st = graph_adapter(root).status()
    except Exception as exc:  # the graph is only an accelerator
        return f"Graph: cannot read status ({_short(exc, 80)}) — using fallback"
    extra = ""
    if st["state"] == "stale":
        extra = f", {len(st['changed'])} files changed → `orc graph update`"
    elif st["problems"]:
        extra = f" — {_short(st['problems'][0], 100)}"
    return f"Graph: {st['state']} ({st['files']} file, {st['nodes']} node){extra}"


def _ctx_block(root: Path, ctx_id: str) -> dict:
    _, body = split_frontmatter(read_text(content_path(root, ctx_id)))
    m = re.search(r"```ya?ml orc-context\s*\n(.*?)```", body, re.S)
    if not m:
        raise OrcError(f"{ctx_id}: missing ```yaml orc-context block")
    data = _yaml().safe_load(m.group(1)) or {}
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise OrcError(f"{ctx_id}: orc-context needs 'files' as a list")
    return data


def ctx_seal(root: Path, ctx_id: str) -> dict:
    """Register the metadata of a CTX written by the Context Builder: source, graph state, hash of each file."""
    if kind_of(ctx_id) != "context":
        raise OrcError(f"{ctx_id} is not a CTX")
    data = _ctx_block(root, ctx_id)
    source = data.get("source")
    if source not in ("graph", "fallback"):
        raise OrcError(f"{ctx_id}: source must be graph|fallback")
    budget = int((load_config(root).get("graph") or {}).get("max_files", CTX_MAX_FILES))
    files, hashes = [], {}
    for item in data["files"]:
        if not isinstance(item, dict) or not item.get("path") or not item.get("reason"):
            raise OrcError(f"{ctx_id}: each file needs a path and a reason")
        rel = str(item["path"]).replace("\\", "/")
        full = root / rel
        if not full.is_file():
            raise OrcError(f"{ctx_id}: file {rel} does not exist")
        files.append(rel)
        hashes[rel] = hashlib.md5(full.read_bytes()).hexdigest()
    if not files:
        raise OrcError(f"{ctx_id}: no file selected")
    if len(files) > budget:
        raise OrcError(f"{ctx_id}: {len(files)} files exceed the budget of {budget}")
    graph_state, graph_fp = "none", None
    try:
        if load_config(root).get("graph"):
            st = graph_adapter(root).status()
            graph_state, graph_fp = st["state"], st["fingerprint"]
    except Exception:  # noqa: BLE001 - a graph error does not block the CTX
        graph_state = "unavailable"
    if source == "graph" and graph_state != "fresh":
        raise OrcError(f"{ctx_id}: source=graph but the graph is {graph_state}; "
                       "update the graph or write source: fallback")
    return set_fields(root, ctx_id, {
        "source": source, "graph_state": graph_state, "paths": files,
        "truncated": bool(data.get("truncated", False)),
        "fingerprint": {"graph": graph_fp, "files": hashes}})


def ctx_check(root: Path, ctx_id: str) -> list:
    """Which files in the CTX changed/disappeared since it was sealed (guards against stale context)."""
    _, _, e = find_entry(root, ctx_id)
    fp = e.get("fingerprint") or {}
    if not fp.get("files"):
        raise OrcError(f"{ctx_id} is not sealed (`orc ctx seal {ctx_id}`)")
    changed = []
    for rel, digest in fp["files"].items():
        full = root / rel
        if not full.is_file():
            changed.append(f"{rel} (missing)")
        elif hashlib.md5(full.read_bytes()).hexdigest() != digest:
            changed.append(f"{rel} (changed)")
    return changed


def _graph_cmd(root: Path, a) -> int:
    from graph_adapter import GraphError  # noqa: PLC0415
    ad = graph_adapter(root)
    try:
        if a.action == "status":
            res = ad.status()
        elif a.action == "update":
            res = ad.update(force=a.force)
        elif a.action == "query":
            res = ad.query(a.args, limit=a.limit)
        elif a.action == "affected":
            res = ad.affected(" ".join(a.args), depth=a.depth, limit=a.limit)
        elif a.action == "explain":
            res = ad.explain(" ".join(a.args), limit=a.limit)
        elif a.action == "view":
            res = ad.view()
            if a.open and res["pages"] and hasattr(os, "startfile"):
                os.startfile(res["pages"][0])  # noqa: S606 - opens a local HTML file
        else:
            if len(a.args) != 2:
                raise OrcError("orc graph path A B")
            res = ad.path(a.args[0], a.args[1])
    except GraphError as exc:
        print(dump_yaml({"state": "unavailable", "authoritative": False, "error": str(exc),
                         "hint": "Use the fallback; the graph is optional."}))
        return 4
    print(dump_yaml(res), end="")
    return 0


# ---------------------------------------------------------------- docs (INDEX.md)

DOC_FIELDS = ("status", "feature", "updated", "summary", "read_when", "supersedes")
DOC_STATUS = ("current", "open", "implemented", "superseded", "archived")
DOC_SCAN_EXT = {".md", ".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".py", ".sql", ".yaml", ".yml"}
DOC_SKIP_DIRS = {"node_modules", "dist", "build", "release", "out", ".git", "__pycache__",
                 "build-tmp", "build-temp", ".venv"}
DOC_REF_RE = re.compile(r"(?<![\w/.-])(docs/[\w./-]+?\.md)\b")
INDEX_START, INDEX_END = "<!-- orc:docs:start -->", "<!-- orc:docs:end -->"


def docs_root(start) -> Path:
    """Project root for docs commands: has .orchestra, or the nearest directory containing docs/ or .git."""
    root = find_root(start)
    if root:
        return root
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "docs").is_dir() or (d / ".git").exists():
            return d
    raise OrcError("No project directory found (needs docs/ or .git)")


def docs_files(root: Path) -> list:
    d = root / "docs"
    return sorted(p for p in d.rglob("*.md") if p.name != "INDEX.md") if d.is_dir() else []


def doc_meta(path: Path):
    try:
        fm, _ = split_frontmatter(read_text(path))
    except Exception as exc:  # broken YAML
        return None, f"frontmatter YAML error ({exc})"
    return fm, None


def _walk_text_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            if d in DOC_SKIP_DIRS or (d.startswith(".") and d not in (".orchestra", ".claude")):
                continue
            if d == "cache" and Path(dirpath).name == ".orchestra":
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in DOC_SCAN_EXT:
                yield p


def docs_check(root: Path) -> tuple:
    """Return (errors, warnings). Errors: bad/missing frontmatter, not in INDEX, broken docs/ paths
    in non-archived files. Broken paths inside archived documents are only warnings (history)."""
    errs, warns = [], []
    index = root / "docs" / "INDEX.md"
    index_text = read_text(index) if index.exists() else ""
    if not index.exists():
        errs.append("docs/INDEX.md is missing")
    archived = set()
    for p in docs_files(root):
        rel = p.relative_to(root).as_posix()
        if index.exists() and f"({rel.removeprefix('docs/')})" not in index_text:
            errs.append(f"{rel}: not in docs/INDEX.md yet (run `orc docs index`)")
        fm, bad = doc_meta(p)
        if bad:
            errs.append(f"{rel}: {bad}")
            continue
        if fm is None:
            errs.append(f"{rel}: missing frontmatter")
            continue
        missing = [f for f in DOC_FIELDS if f not in fm]
        if missing:
            errs.append(f"{rel}: frontmatter is missing {missing}")
        if fm.get("status") not in DOC_STATUS:
            errs.append(f"{rel}: status '{fm.get('status')}' must be {'|'.join(DOC_STATUS)}")
        if len(str(fm.get("summary") or "")) > SUMMARY_MAX:
            errs.append(f"{rel}: summary > {SUMMARY_MAX} characters")
        if not str(fm.get("summary") or "").strip():
            errs.append(f"{rel}: missing summary")
        for s in as_list(fm.get("supersedes")):
            if not (root / str(s)).exists():
                errs.append(f"{rel}: supersedes {s}, which does not exist")
        if fm.get("status") in ("archived", "superseded"):
            archived.add(rel)
        if fm.get("status") in ("archived", "superseded") and not rel.startswith("docs/archive/"):
            errs.append(f"{rel}: status {fm.get('status')} must live in docs/archive/ "
                        "(outdated content in one place)")
    for p in _walk_text_files(root):
        rel = p.relative_to(root).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for ref in sorted(set(DOC_REF_RE.findall(text))):
            if not (root / ref).exists():
                msg = f"{rel}: broken path {ref}"
                (warns if rel in archived else errs).append(msg)
    return errs, warns


def _md_cell(text) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


def docs_index(root: Path) -> str:
    """Regenerate the automatic part of docs/INDEX.md from frontmatter. The hand-written part is kept."""
    groups = {}
    for p in docs_files(root):
        rel = p.relative_to(root / "docs").as_posix()
        fm, _ = doc_meta(p)
        fm = fm or {}
        parts = rel.split("/")
        if parts[0] == "features" and len(parts) > 2:
            group = "/".join(parts[:2])
        elif len(parts) > 1:
            group = parts[0]
        else:
            group = "(docs root)"
        groups.setdefault(group, []).append((rel, fm, p.stat().st_size))
    fixed = ("architecture", "decisions", "audits", "archive")
    order = (["architecture", "decisions"] + sorted(g for g in groups if g.startswith("features/"))
             + ["audits"] + sorted(g for g in groups if g not in fixed and not g.startswith("features/"))
             + ["archive"])
    lines = [INDEX_START, "",
             "<!-- Generated by `orc docs index` from frontmatter. Do not edit by hand; edit the frontmatter and rerun. -->",
             ""]
    for g in order:
        if g not in groups:
            continue
        lines += [f"### {g}", "", "| File | Status | Read when | Summary | KB |", "|---|---|---|---|---|"]
        for rel, fm, size in groups[g]:
            lines.append(f"| [{rel}]({rel}) | {_md_cell(fm.get('status'))} | {_md_cell(fm.get('read_when'))} | "
                         f"{_md_cell(fm.get('summary'))} | {max(1, round(size / 1024))} |")
        lines.append("")
    if find_root(root) is not None:
        decs = [d for d in load_registry(root, "decision") if d.get("status") == "accepted"]
        if decs:
            lines += ["### Recent decisions (orchestra)", ""]
            lines += [f"- {d['id']} ({_md_cell(d.get('scope'))}): {_md_cell(d.get('summary'))}" for d in decs]
            lines.append("")
    lines.append(INDEX_END)
    block = "\n".join(lines)
    index = root / "docs" / "INDEX.md"
    text = read_text(index) if index.exists() else "# INDEX — documentation map\n\n"
    if INDEX_START in text and INDEX_END in text:
        head, rest = text.split(INDEX_START, 1)
        _, tail = rest.split(INDEX_END, 1)
        text = head + block + tail
    else:
        text = text.rstrip() + "\n\n## Catalog by group (generated)\n\n" + block + "\n"
    write_text(index, text)
    return f"updated {index.relative_to(root).as_posix()} ({sum(len(v) for v in groups.values())} files)"


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orc", description="orchestra-kit CLI")
    p.add_argument("-C", dest="cwd", default=".", help="working directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create .orchestra/")
    s.add_argument("--project-id", required=True)
    s.add_argument("--goal", default="")

    s = sub.add_parser("brief", help="summary of at most ~1.5k tokens")
    s.add_argument("--key", help="Claude session_id")

    s = sub.add_parser("new", help="create a new entry, print its ID")
    s.add_argument("type", choices=("task", "request", "artifact", "decision", "context", "evidence"))
    s.add_argument("--title")
    s.add_argument("--owner")
    s.add_argument("--summary", default="")
    s.add_argument("--task")
    s.add_argument("--type", dest="art_type")
    s.add_argument("--supersedes")
    s.add_argument("--depends-on", nargs="*")
    s.add_argument("--blocked-by", nargs="*")
    s.add_argument("--acceptance", nargs="*")
    s.add_argument("--write-scope", nargs="*")
    s.add_argument("--inputs", nargs="*")
    s.add_argument("--context", nargs="*")
    s.add_argument("--scope", default="project")
    s.add_argument("--status")
    s.add_argument("--kind", default="test")
    s.add_argument("--result", default="info")
    s.add_argument("--paths", nargs="*")
    s.add_argument("--from", dest="from_plan", help="create tasks from an accepted plan ART")
    s.add_argument("--parent", help="K root task (required in workflow: k projects for non-root tasks)")

    s = sub.add_parser("set", help="edit fields: orc set ID key=value ...")
    s.add_argument("id")
    s.add_argument("pairs", nargs="+")

    s = sub.add_parser("advance", help="change status by the rules: orc advance ID --to X")
    s.add_argument("id")
    s.add_argument("--to", required=True)

    s = sub.add_parser("triage", help="deterministic level (shadow): run ID --stage 1|2|3 [--form F] | show ID | "
                                      "downgrade ID --to N --reason R")
    s.add_argument("action", choices=("run", "show", "downgrade"))
    s.add_argument("id")
    s.add_argument("--stage", type=int, default=2)
    s.add_argument("--form", help="YAML yes/no: schema_change, data_migration, security, irreversible, "
                                  "public_api, new_dependency, external_service, user_visible, uncertain")
    s.add_argument("--paths", nargs="*", help="expected paths (stage 1)")
    s.add_argument("--to", type=int)
    s.add_argument("--reason", default="")

    s = sub.add_parser("resume", help="K root task: BLOCKED → previous status, once the user has typed `orc:resume ID`")
    s.add_argument("id")

    s = sub.add_parser("abort", help="K root task → ABORTED, once the user has typed `orc:abort ID`")
    s.add_argument("id")

    s = sub.add_parser("approve", help="record an approval once the user has typed `orc:approve ID,... NONCE` (DEC/ART → accepted)")
    s.add_argument("id", nargs="+")

    s = sub.add_parser("approval", help="request ID...: issue a nonce + the line the user must type | check TASK: still valid?")
    s.add_argument("action", choices=("request", "check"))
    s.add_argument("id", nargs="+")

    s = sub.add_parser("verify", help="deterministic V1/V2: scope via git + check commands from the checkpoint (exit 6 = fail)")
    s.add_argument("id")

    s = sub.add_parser("show", help="show one ID")
    s.add_argument("id")
    s.add_argument("--meta", action="store_true", help="metadata only (for the Orchestrator)")
    s.add_argument("--pack", action="store_true", help="package for the reviewer")

    sub.add_parser("next", help="pick the next task (deterministic)")

    s = sub.add_parser("apply", help="load a subagent's YAML envelope (file or -)")
    s.add_argument("file")

    s = sub.add_parser("handoff", help="open/check the session handoff")
    s.add_argument("action", choices=("open", "check"))
    s.add_argument("--key")
    s.add_argument("--task")
    s.add_argument("--role", default="orchestrator")
    s.add_argument("--request", default="")
    s.add_argument("--sid")

    s = sub.add_parser("gate", help="can the plan proceed on its own (exit 3 = must ask the user)")
    s.add_argument("id")

    s = sub.add_parser("report", help="report for one request (root TASK)")
    s.add_argument("id")

    s = sub.add_parser("ask", help="extract the understanding + questions of a research ART for the user")
    s.add_argument("id")

    s = sub.add_parser("graph", help="code graph (optional): status|update|query|affected|explain|path")
    s.add_argument("action", choices=("status", "update", "query", "affected", "explain", "path", "view"))
    s.add_argument("args", nargs="*")
    s.add_argument("--force", action="store_true")
    s.add_argument("--limit", type=int)
    s.add_argument("--depth", type=int, default=2)
    s.add_argument("--open", action="store_true", help="view: open the map in the browser")

    s = sub.add_parser("docs", help="docs/INDEX.md: index (regenerate) | check")
    s.add_argument("action", choices=("index", "check"))

    s = sub.add_parser("ctx", help="context: seal|check CTX-x")
    s.add_argument("action", choices=("seal", "check"))
    s.add_argument("id")

    s = sub.add_parser("validate", help="validate all of .orchestra/")
    s.add_argument("--handoff", action="store_true", help="also check the open session's handoff")
    s.add_argument("--key")
    return p


def run(argv=None) -> int:
    args = build_parser().parse_args(argv)
    start = Path(args.cwd)
    if safe_mode() and ((args.cmd, None) in SAFE_BLOCKED
                        or (args.cmd, getattr(args, "action", None)) in SAFE_BLOCKED):
        raise OrcError(f"ORC_SAFE_MODE: `orc {args.cmd}` changes the workflow/project and is refused. "
                       "Only the kit/.orchestra/config.yaml may be edited; the user turns ORC_SAFE_MODE off and reruns.")
    if args.cmd == "init":
        for path in cmd_init(start.resolve(), args.project_id, args.goal):
            print(f"created {path}")
        return 0
    if args.cmd == "docs":  # also works in projects without .orchestra/
        droot = docs_root(start)
        if args.action == "index":
            print(docs_index(droot))
            return 0
        errs, warns = docs_check(droot)
        for w in warns:
            print(f"warning: {w}")
        if errs:
            print("\n".join(errs))
            return 1
        print(f"OK ({len(docs_files(droot))} documents, {len(warns)} warnings)")
        return 0
    root = require_root(start)
    lock = (state_lock(root) if args.cmd in MUTATING else contextlib.nullcontext())
    with lock, journal_context(actor=f"orc {args.cmd}"):
        try:
            return _run(root, args)
        except OrcError as exc:
            if args.cmd in MUTATING or args.cmd == "verify":
                with (contextlib.nullcontext() if args.cmd in MUTATING else state_lock(root)):
                    record_rejection(root, args, exc)
            raise


def record_rejection(root: Path, args, exc: Exception) -> None:
    """A refused state-writing command ⇒ one `rejected` line in the journal (hash chain). Observation only:
    the error is re-raised unchanged, the exit code does not change. A failing journal write is ignored (never hides the original error)."""
    reason = str(exc)
    op = " ".join(str(x) for x in (args.cmd, getattr(args, "action", None) or getattr(args, "type", None)) if x)
    task = getattr(args, "task_hint", None) or getattr(args, "id", None) or getattr(args, "task", None)
    task = ",".join(task) if isinstance(task, list) else task
    try:
        with journal_context(extra={}):
            append_journal(root, [{"event": "rejected", "task": task, "operation": op, "reason": reason[:500],
                                   "reason_hash": hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]}])
    except Exception:  # noqa: BLE001
        pass


# Commands that write state/registry: run inside the lock
MUTATING = {"new", "set", "advance", "apply", "next", "handoff", "ctx", "approve", "approval", "resume", "abort",
            "triage"}
# verify takes the lock only while writing EVD: long-running check commands must not hold the state lock


def _run(root: Path, args) -> int:
    if args.cmd == "brief":
        print(brief_text(root, key=args.key))
    elif args.cmd == "new":
        print(_cmd_new(root, args))
    elif args.cmd == "set":
        guarded_set(root, args.id, parse_assignments(args.pairs))
        print(f"updated {args.id}")
    elif args.cmd == "advance":
        e = advance(root, args.id, args.to)
        print(f"{args.id} → {e['status']}")
        if e.get("_note"):
            print(e["_note"])
    elif args.cmd == "triage":
        import orc_triage  # noqa: PLC0415
        if args.action == "show":
            sys.stdout.write(orc_triage.show(root, args.id))
        elif args.action == "downgrade":
            if args.to not in (0, 1, 2, 3):
                raise OrcError("downgrade needs --to 0..3")
            rec = orc_triage.downgrade(root, args.id, args.to, args.reason)
            print(f"{args.id}: lowered L{rec['previous']} → L{rec['level']} (shadow)")
        else:
            rec = orc_triage.run(root, args.id, args.stage, form=orc_triage.load_form(args.form),
                                 paths=args.paths)
            print(orc_triage.render(rec))
    elif args.cmd in ("resume", "abort"):
        import orc_k  # noqa: PLC0415
        e = (orc_k.resume if args.cmd == "resume" else orc_k.abort)(root, args.id)
        print(f"{args.id} → {e['status']}")
    elif args.cmd == "approve":
        import orc_approval  # noqa: PLC0415
        req = orc_approval.approve(root, args.id)
        print(f"{','.join(req['ids'])}: approved (nonce {req['nonce']}, user prompt {req['prompt_sha256'][:12]}…)")
    elif args.cmd == "approval":
        import orc_approval  # noqa: PLC0415
        if args.action == "request":
            print(orc_approval.request(root, args.id))
        else:
            ok, why = orc_approval.check(root, args.id[0])
            print(("OK: " if ok else "NO: ") + why)
            return 0 if ok else 3
    elif args.cmd == "verify":
        import orc_verify  # noqa: PLC0415
        res = orc_verify.verify(root, args.id)
        sys.stdout.write(orc_verify.render(res))
        return orc_verify.EXIT_CODE[orc_verify.overall(res)]  # 0 PASS, 6 FAIL, 7 INCOMPLETE (environment)
    elif args.cmd == "show":
        sys.stdout.write(show(root, args.id, meta=args.meta, pack=args.pack))
    elif args.cmd == "next":
        print(next_report(root))
    elif args.cmd == "apply":
        text = (sys.stdin.buffer.read().decode("utf-8-sig") if args.file == "-"
                else read_text(Path(args.file)))
        m = re.search(r"^\s*task_id:\s*['\"]?([A-Z]+-\d+)", text, re.M)
        args.task_hint = m.group(1) if m else None  # for the journal `rejected` line
        print(apply_envelope(root, parse_envelope(text)))
    elif args.cmd == "handoff":
        if args.action == "open":
            print(handoff_open(root, args.role, task=args.task, key=args.key,
                               request=args.request))
        else:
            sid = args.sid or (open_marker(root, args.key) or {}).get("sid")
            if not sid:
                raise OrcError("Needs --sid or --key of the open session")
            errs = handoff_errors(root, sid)
            if errs:
                print("\n".join(errs))
                return 1
            if args.key:
                close_marker(root, args.key)
            print(f"{sid}: handoff is valid")
    elif args.cmd == "gate":
        reasons = gate(root, args.id)
        if reasons:
            print("ASK\n- " + "\n- ".join(reasons))
            return 3
        print("PROCEED")
    elif args.cmd == "report":
        sys.stdout.write(report(root, args.id))
    elif args.cmd == "ask":
        sys.stdout.write(ask_text(root, args.id))
    elif args.cmd == "graph":
        return _graph_cmd(root, args)
    elif args.cmd == "ctx":
        if args.action == "seal":
            e = ctx_seal(root, args.id)
            print(f"{args.id}: {e['source']}, graph {e['graph_state']}, {len(e['paths'])} file"
                  + (", truncated" if e["truncated"] else ""))
        else:
            changed = ctx_check(root, args.id)
            if changed:
                print("STALE CTX — reread these files from disk:\n- " + "\n- ".join(changed))
                return 5
            print(f"{args.id}: the files in the context are unchanged")
    elif args.cmd == "validate":
        errs = validate(root)
        if args.handoff:
            if args.key:
                m = open_marker(root, args.key)
                if m:
                    errs += handoff_errors(root, m["sid"])
            else:
                rt = orc(root) / "runtime" / "sessions"
                for mp in rt.glob("*.yaml") if rt.is_dir() else []:
                    m = load_yaml_file(mp) or {}
                    if not m.get("closed"):
                        errs += handoff_errors(root, m["sid"])
        if errs:
            print("\n".join(errs))
            return 1
        print("OK")
    return 0


def _cmd_new(root: Path, a) -> str:
    if a.type == "request":
        if not a.title:
            raise OrcError("new request needs --title")
        return new_task(root, title=a.title, owner="orchestrator", summary=a.summary, workflow="k",
                        status="NEW")["id"]
    if a.type == "task":
        if a.from_plan:
            return "\n".join(t["id"] for t in tasks_from_plan(root, a.from_plan))
        if not a.title:
            raise OrcError("new task needs --title")
        if load_config(root).get("workflow") == "k" and not a.parent:
            raise OrcError("workflow k project: a new task needs `--parent <root task>` (create the root with `orc new request`)")
        return new_task(root, title=a.title, owner=a.owner or "coder", summary=a.summary, parent=a.parent,
                        blocked_by=a.blocked_by, acceptance=a.acceptance,
                        write_scope=a.write_scope, inputs=a.inputs, context=a.context)["id"]
    if a.type == "artifact":
        if not a.art_type or not a.task:
            raise OrcError("new artifact needs --type and --task")
        return new_artifact(root, a.art_type, a.task, summary=a.summary,
                            supersedes=a.supersedes, depends_on=a.depends_on)["id"]
    if a.type == "decision":
        if not a.summary:
            raise OrcError("new decision needs --summary")
        if (a.status or "proposed") != "proposed":
            raise OrcError(f"`orc new decision --status {a.status}` refused: the LLM only proposes (proposed); "
                           "a DEC becomes accepted through `orc approval request DEC-x` + the user typing `orc:approve ...`")
        return new_decision(root, a.summary, scope=a.scope, supersedes=a.supersedes,
                            status=a.status or "proposed")["id"]
    if a.type == "context":
        return new_context(root, a.task, a.summary, paths=a.paths)["id"]
    return new_evidence(root, a.task, a.summary, kind=a.kind, result=a.result,
                        paths=a.paths)["id"]


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    try:
        return run(argv)
    except OrcError as exc:
        print(f"orc: {exc}", file=sys.stderr)
        return 1
