"""PreToolUse guard of orchestra-kit. Stdlib only, never loads YAML.

Principle: **Bash is not a semantic sandbox.** The guard only denies dangerous operations it can recognize
with high confidence; everything else is checked afterwards by `orc verify` (git diff/status, risk paths). It does not infer
write_scope from Bash semantics; write_scope is checked directly only for Edit/Write. This is a boundary + detection,
not a sandbox: scripts/`python -c` can still write, V2 catches it afterwards.

- Edit/Write: `deny_always` (orchestra state/runtime/..., the project's `.claude/` and `~/.claude/`, `.git/`, secret files,
  the config's `protected_paths`) except permitted artifact/context/session files; while a task runs: only write_scope
  (coder task) or permitted files.
- Bash/PowerShell (all modes): strip heredoc bodies and quoted phrases containing spaces, then match high-confidence deny rules
  (destructive git, DB, publish/deploy, recursive delete, writes into deny_always areas, reading secret files).
- Read/Grep: block reading secret files. Web: not while editing/checking code; during research only through researcher-external.
- Agent/SubagentHandback/…: no writes ⇒ allowed. Unknown tools: allowed, logged as "unclassified tool".
SAFE_MODE (`ORC_SAFE_MODE=1`): Edit/Write only the kit + `.orchestra/config.yaml`; state is not read.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
WEB_TOOLS = {"WebFetch", "WebSearch"}
READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead", "LS"}
# Tools that do not write project files (seen in shadow runs or provided by Claude Code)
PASS_TOOLS = {"Agent", "Task", "SubagentHandback", "SendMessage", "TodoWrite", "ToolSearch", "AskUserQuestion",
              "Skill", "ExitPlanMode", "EnterPlanMode", "BashOutput", "KillShell", "ListAgents"}

# High-authority areas, relative to the project root (gitignore-style, see match_path)
DENY_PATHS = [".orchestra/", ".claude/", ".git/", "**/.git/"]
SECRET_PATHS = ["**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/id_rsa*", "**/id_ed25519*", "**/secrets/"]
SECRET_OK = re.compile(r"\.(example|sample|template)$")

DENY_SHELL = [
    ("git push", re.compile(r"\bgit\b(?:\s+-[Cc]\s+\S+)*\s+push\b")),
    ("git merge", re.compile(r"\bgit\b(?:\s+-[Cc]\s+\S+)*\s+merge\b")),
    ("git rebase", re.compile(r"\bgit\b(?:\s+-[Cc]\s+\S+)*\s+rebase\b")),
    ("git filter-branch", re.compile(r"\bgit\b(?:\s+-[Cc]\s+\S+)*\s+filter-(?:branch|repo)\b")),
    ("git reset --hard", re.compile(r"\bgit\b[^;&|\n]*\breset\s+(?:\S+\s+)*--hard\b")),
    ("git clean -f", re.compile(r"\bgit\b[^;&|\n]*\bclean\s+-\w*f")),
    ("git branch -D", re.compile(r"\bgit\b[^;&|\n]*\bbranch\s+(?:\S+\s+)*-D\b")),
    ("supabase db push/reset", re.compile(r"\bsupabase\s+db\s+(?:push|reset)\b")),
    ("supabase deploy/secrets", re.compile(r"\bsupabase\s+(?:functions\s+deploy|secrets\s+set)\b")),
    ("publish", re.compile(r"\b(?:npm|pnpm|yarn)\s+publish\b")),
    ("deploy", re.compile(r"\b(?:firebase|netlify)\s+deploy\b|\bvercel\b[^;&|\n]*--prod\b"
                          r"|\bgh\s+(?:release\s+create|pr\s+merge|workflow\s+run)\b")),
    ("recursive delete", re.compile(r"\brm\s+(?:-\w*[rR]\w*|--recursive)\b|\bRemove-Item\b[^;&|\n]*-Recurse"
                             r"|\b(?:rmdir|rd|del)\s+/[sS]\b|\bfind\b[^;&|\n]*\s-delete\b", re.I)),
]
PSQL = re.compile(r"\bpsql\b")
LOCAL = re.compile(r"localhost|127\.0\.0\.1|::1")
# Heredoc body up to its terminator line, or to the end of the command if there is none (truncated command)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?(?:\n\s*\2\b|\Z)", re.S)
# Quoted strings; an unclosed quote (truncated/invalid command) runs to the end of the command
QUOTED = re.compile(r"'[^'\n]*(?:'|$)|\"(?:[^\"\\]|\\.)*(?:\"|$)", re.S)
SEGMENT = re.compile(r"&&|\|\||;|\||\n")
WRITE_OPS = re.compile(r"(?:^|\s)(?:\d?>>?|tee|sed|rm|mv|cp|touch|truncate|Set-Content|Add-Content|Out-File|"
                       r"New-Item|Copy-Item|Move-Item|Remove-Item|Clear-Content)(?=\s|$)")
REDIRECT = re.compile(r"(?<![<\d&])\d?>>?(?!&)\s*(\S+)")


# ---------------------------------------------------------------- path matching

def _norm(p: str) -> str:
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _glob_re(pat: str) -> str:
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return "".join(out)


def match_path(path: str, patterns) -> bool:
    """gitignore-style matching **anchored at the project root**: `*`/`?` do not cross `/`; `**` crosses levels; a pattern ending in `/` is
    a directory (matches everything below); other patterns match that file or everything below a directory of that name."""
    p = _norm(path).rstrip("/")
    for raw in patterns or []:
        s = _norm(raw)
        if not s:
            continue
        base = _glob_re(s.rstrip("/"))
        if re.fullmatch(base + r"(?:/.*)?", p):
            return True
    return False


# ---------------------------------------------------------------- helpers

def bare_agent(name) -> str:
    """Agent name without the plugin namespace: "orchestra:coder" → "coder" (the legacy install used bare names)."""
    s = str(name or "")
    return s[len("orchestra:"):] if s.startswith("orchestra:") else s


def find_root(start):
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / ".orchestra" / "config.yaml").is_file():
            return d
    return None


def safe_mode() -> bool:
    return os.environ.get("ORC_SAFE_MODE", "").strip().lower() in ("1", "true", "on", "yes")


def rel_path(root: Path, raw: str):
    """Path relative to the project root (posix), or None if outside the project."""
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = root / p
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def under(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def home_claude(raw: str) -> bool:
    return under(Path(os.path.expanduser(raw)), Path.home() / ".claude")


def is_secret(path: str) -> bool:
    return match_path(path, SECRET_PATHS) and not SECRET_OK.search(path)


def target_of(tool: str, ti: dict) -> str:
    if tool in WRITE_TOOLS or tool in READ_TOOLS:
        return str(ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or "")
    if tool in SHELL_TOOLS:
        return str(ti.get("command") or "")
    if tool in WEB_TOOLS:
        return str(ti.get("url") or ti.get("query") or "")
    if tool in ("Agent", "Task"):
        return str(ti.get("subagent_type") or ti.get("description") or "")
    return ""


def modes(active: dict) -> dict:
    tasks = active.get("tasks") or []
    impl = [t for t in tasks if t.get("status") == "dispatched" and str(t.get("owner", "")).startswith("coder")]
    research = [t for t in tasks if t.get("status") == "dispatched" and t not in impl]
    review = [t for t in tasks if t.get("status") == "in_review"]
    return {"implement": impl, "research": research, "verify": review}


# ---------------------------------------------------------------- Bash

def shell_view(cmd: str) -> str:
    """Strip heredoc bodies; quoted phrases with spaces become Q, quotes around a single word (a path) are kept.
    Not a shell parser — only keeps deny rules from matching data/text."""
    text = HEREDOC.sub("<<HEREDOC", cmd)

    def q(m):
        inner = m.group(0)[1:-1]
        return "Q" if (not inner or re.search(r"\s", inner)) else inner
    return QUOTED.sub(q, text)


def write_targets(view: str) -> list:
    """Explicit write targets: after `>`/`>>` redirection, and arguments of tee/sed -i/rm/mv/cp/… in the same command segment."""
    out = [m.group(1) for m in REDIRECT.finditer(view)]
    for seg in SEGMENT.split(view):
        if WRITE_OPS.search(seg) and not seg.strip().startswith(("cat ", "echo ")):
            m = WRITE_OPS.search(seg)
            out += [w for w in seg[m.end():].split() if not w.startswith("-") and w not in ("Q", "<<HEREDOC")]
    return [t for t in out if t not in ("/dev/null", "$null", "nul", "NUL", "&1", "&2")]


def protected_hit(root: Path, raw: str, writable, protected) -> str | None:
    """Reason if the path is inside a deny_always area, otherwise None."""
    if home_claude(raw):
        return "~/.claude"
    rel = rel_path(root, raw)
    if rel is None:
        return None
    if rel in writable:
        return None
    if match_path(rel, DENY_PATHS + list(protected or [])):
        return rel
    if is_secret(rel):
        return f"secret file {rel}"
    return None


def decide_shell(root: Path, cmd: str, active: dict | None) -> tuple:
    view = shell_view(cmd)
    for label, rx in DENY_SHELL:
        if rx.search(view):
            return "deny", f"deny_always: {label}"
    if PSQL.search(view) and not LOCAL.search(cmd):
        return "deny", "deny_always: psql to a host other than localhost"
    writable = set((active or {}).get("writable") or [])
    protected = (active or {}).get("protected") or []
    for t in write_targets(view):
        hit = protected_hit(root, t, writable, protected)
        if hit:
            return "deny", f"deny_always: write {hit}"
    for tok in view.split():
        tok = tok.strip("();|&<>")
        if tok and is_secret(_norm(tok)):
            return "deny", f"deny_always: touches secret file {tok}"
    return "allow", "matches no high-confidence deny rule (checked afterwards by orc verify)"


# ---------------------------------------------------------------- decision

def decide(root: Path, active, tool: str, ti: dict, agent_type: str | None = None,
           safe: bool | None = None, kit: Path = KIT_DIR) -> tuple:
    """(allow|deny, reason, mode)."""
    safe = safe_mode() if safe is None else safe
    target = target_of(tool, ti)
    if safe:
        if tool in WRITE_TOOLS:
            p = Path(target) if Path(target).is_absolute() else root / target
            if under(p, kit) or rel_path(root, target) == ".orchestra/config.yaml":
                return "allow", "SAFE_MODE: editing the kit/orchestra config", "safe"
            return "deny", "SAFE_MODE: only the kit and .orchestra/config.yaml may be edited", "safe"
        if tool in SHELL_TOOLS:
            return (*decide_shell(root, target, None), "safe")
        return "allow", "SAFE_MODE: tool does not write", "safe"

    if active is None:
        raise RuntimeError("runtime/active.json is missing")  # enforcing mode: fail-closed
    m = modes(active)
    mode = ("implement" if m["implement"] else "research" if m["research"]
            else "verify" if m["verify"] else "normal")
    writable = set(active.get("writable") or [])
    if tool in WRITE_TOOLS:
        hit = protected_hit(root, target, writable, active.get("protected"))
        if hit:
            return "deny", f"deny_always: write {hit}", mode
        rel = rel_path(root, target)
        if rel is None:
            if under(Path(target), Path(tempfile.gettempdir())):
                return "allow", "system temp directory", mode
            if mode == "normal":
                return "allow", "no task running (outside the project)", mode
            return "deny", f"{mode}: write outside the project", mode
        if mode == "normal" or rel in writable:
            return "allow", ("no task running" if mode == "normal"
                             else f"{mode}: permitted artifact/context file"), mode
        scope = [s for t in m["implement"] for s in t.get("write_scope") or []]
        if scope and match_path(rel, scope):
            return "allow", f"{mode}: inside write_scope", mode
        return "deny", f"{mode}: {rel} is outside the write_scope/artifacts of the running task", mode
    if tool in SHELL_TOOLS:
        return (*decide_shell(root, target, active), mode)
    if tool in READ_TOOLS:
        rel = rel_path(root, target) if target else None
        if (rel and is_secret(rel)) or (target and is_secret(_norm(target))):
            return "deny", f"deny_always: reading secret file {target}", mode
        return "allow", "read", mode
    if tool in WEB_TOOLS:
        if mode in ("implement", "verify"):
            return "deny", f"{mode}: no web while editing/checking code", mode
        if mode == "research" and bare_agent(agent_type) != "researcher-external":
            return "deny", "research: web only through the researcher-external subagent", mode
        return "allow", f"{mode}: web", mode
    if tool in PASS_TOOLS:
        return "allow", "tool does not write files", mode
    return "allow", f"unclassified tool: {tool}", mode


def load_active(root: Path):
    path = root / ".orchestra" / "runtime" / "active.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def refresh_active(root: Path) -> None:
    """Shadow on projects still running a pre-0.2 `orc` (which does not write active.json): rebuild it from the registry when stale.
    Writes only `runtime/active.json` (gitignored), never changes the workflow. Enforcing mode does not use this path."""
    o = root / ".orchestra"
    act = o / "runtime" / "active.json"
    src = [o / "state" / "tasks.yaml", o / "state" / "artifacts.yaml", o / "state" / "contexts.yaml"]
    newest = max((p.stat().st_mtime for p in src if p.exists()), default=0)
    if act.exists() and act.stat().st_mtime >= newest:
        return
    import orc_core  # noqa: PLC0415 - loads YAML only when the registry changed
    orc_core.write_active(root)


def shadow(payload: dict) -> dict | None:
    """Hook logic in shadow mode. Returns the logged record (for tests), None if this is not an orchestra project."""
    t0 = time.perf_counter()
    root = find_root(payload.get("cwd") or ".")
    if root is None:
        return None
    tool, ti = payload.get("tool_name") or "", payload.get("tool_input") or {}
    try:
        if not safe_mode():
            refresh_active(root)
        decision, reason, mode = decide(root, None if safe_mode() else load_active(root), tool, ti,
                                        payload.get("agent_type"))
    except Exception as exc:  # noqa: BLE001 - enforcing mode will exit 2
        decision, reason, mode = "deny", f"guard error (fail-closed): {exc}", "error"
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": payload.get("session_id"),
           "agent_type": payload.get("agent_type"), "tool": tool, "target": target_of(tool, ti)[:200],
           "mode": mode, "decision": decision, "reason": reason,
           "ms": round((time.perf_counter() - t0) * 1000, 1)}
    log = root / ".orchestra" / "runtime" / "guard-shadow.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


# ---------------------------------------------------------------- user prompt log (UserPromptSubmit hook)

ORC_LINE = re.compile(r"^\s*(orc:[a-z]+\b.*?)\s*$", re.M)


def _sha(text: str) -> str:
    import hashlib  # noqa: PLC0415
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompts_path(root: Path) -> Path:
    return root / ".orchestra" / "runtime" / "prompts.jsonl"


def log_prompt(payload: dict) -> dict | None:
    root = find_root(payload.get("cwd") or ".")
    if root is None:
        return None
    prompt = str(payload.get("prompt") or "")
    path = prompts_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []
    from datetime import datetime  # noqa: PLC0415
    rec = {"ts": datetime.now().isoformat(timespec="microseconds"), "session": payload.get("session_id"),
           "sha256": _sha(prompt), "orc": ORC_LINE.findall(prompt),
           "prev": _sha(lines[-1]) if lines else "0" * 64}
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return rec
