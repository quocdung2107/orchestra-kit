"""State machine K for requests in `workflow: k` projects.

Design:
- The **root task** of a request (`workflow: k`, created with `orc new request`) follows K:
  NEW → TRIAGED → RESEARCHING → PLANNED → READY_FOR_IMPLEMENTATION (L0/L1) | APPROVAL_REQUIRED (L2/L3, or no
  level yet) → IMPLEMENTING → VERIFYING → DONE → INTEGRATED; side states BLOCKED · FAILED · ABORTED.
  The understanding & design stage always stops for the user ⇒ there is no TRIAGED → PLANNED.
- **Child tasks** (`parent`) keep the short loop pending → ready → dispatched → in_review → done, but:
  a coder task is dispatched only while the root is IMPLEMENTING and the root's approval is still valid; it is done
  only when the `orc verify` run after its submission PASSes. Failures follow table I2 (RETRY/REPAIR/ROLLBACK/ESCALATE/ABORT); ROLLBACK only prints commands for the user.
- BLOCKED → previous status: `orc resume` once the user typed `orc:resume ROOT`; ABORTED: `orc abort` once the user typed `orc:abort ROOT`.
"""

from __future__ import annotations

import json
from pathlib import Path

import orc_core as oc

K_STATES = ("NEW", "TRIAGED", "RESEARCHING", "PLANNED", "READY_FOR_IMPLEMENTATION", "APPROVAL_REQUIRED",
            "IMPLEMENTING", "VERIFYING", "DONE", "INTEGRATED", "BLOCKED", "FAILED", "ABORTED")
CLOSED = ("done", "aborted", "DONE", "INTEGRATED", "ABORTED")
K_MOVES = {
    ("NEW", "TRIAGED"), ("TRIAGED", "RESEARCHING"), ("RESEARCHING", "PLANNED"),
    ("PLANNED", "READY_FOR_IMPLEMENTATION"), ("PLANNED", "APPROVAL_REQUIRED"),
    ("READY_FOR_IMPLEMENTATION", "IMPLEMENTING"), ("APPROVAL_REQUIRED", "IMPLEMENTING"),
    ("IMPLEMENTING", "VERIFYING"), ("VERIFYING", "DONE"), ("VERIFYING", "IMPLEMENTING"), ("DONE", "INTEGRATED"),
}
RECOVERY = ("RETRY", "REPAIR", "ROLLBACK", "ESCALATE", "ABORT")
VERIFY_FAILURE = {"test": "TEST_FAILURE", "lint": "LINT_FAILURE", "build": "BUILD_FAILURE",
                  "typecheck": "TYPECHECK_FAILURE"}


def is_root(t: dict) -> bool:
    return (t or {}).get("workflow") == "k"


def is_closed(status) -> bool:
    return status in CLOSED


def k_project(root: Path) -> bool:
    return oc.load_config(root).get("workflow") == "k"


def parent_of(root: Path, t: dict):
    p = t.get("parent")
    return oc.find_entry(root, p)[2] if p else None


def children(root: Path, rid: str, tasks=None) -> list:
    return [t for t in (tasks or oc.load_registry(root, "task")) if t.get("parent") == rid]


def coders(root: Path, rid: str, tasks=None) -> list:
    """Live coder tasks of the request (aborted tasks do not count; an abort changes the binding ⇒ re-approval needed)."""
    return [t for t in children(root, rid, tasks)
            if oc.agent_for(t.get("owner")) == "coder" and t.get("status") != "aborted"]


def child_arts(root: Path, rid: str, type_: str) -> list:
    kids = {t["id"] for t in children(root, rid)}
    return [a for a in oc.load_registry(root, "artifact") if a.get("task") in kids and a.get("type") == type_]


def request_ids(root: Path, rid: str) -> list:
    """Every ID belonging to the request (root, child tasks, their artifacts) — to gather proposed DECs into one approval."""
    kids = [t["id"] for t in children(root, rid)]
    arts = [a["id"] for a in oc.load_registry(root, "artifact") if a.get("task") in set(kids)]
    return [rid, *kids, *arts]


# ---------------------------------------------------------------- I2: failure_class → recovery_action

def recovery(fc: str, level, attempts: int) -> str:
    """Table I2 (deterministic). level None = no level yet ⇒ treated as L2 (small is not safe by default)."""
    lv = 2 if level is None else int(level)
    if fc in ("ENVIRONMENT_FAILURE", "VALIDATION"):
        return "ESCALATE"  # automatic RETRY only when idempotent; orc never reruns on its own — a person decides
    if fc in ("TEST_FAILURE", "LINT_FAILURE", "BUILD_FAILURE", "TYPECHECK_FAILURE", "IMPL_BUG"):
        return "REPAIR" if lv <= 2 and attempts <= oc.MAX_ATTEMPTS else "ESCALATE"
    if fc in ("REGRESSION", "SCOPE_VIOLATION"):
        return "ROLLBACK"  # then ESCALATE
    if fc == "SPEC_GAP":
        return "REPAIR" if lv <= 1 else "ESCALATE"
    if fc == "INTERNAL":
        return "ABORT"
    return "ESCALATE"  # DECISION_CONFLICT, CONTEXT_CONFLICT, APPROVAL_MISSING, REVIEW_REJECT, others


# ---------------------------------------------------------------- root task

def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise oc.OrcError(msg)


def advance_root(root: Path, e: dict, to: str) -> tuple:
    """Move a root task through the K table, checking conditions. Returns (entry, note to print for the Orchestrator)."""
    rid, frm = e["id"], e.get("status")
    if (frm, to) not in K_MOVES:
        allowed = sorted(t for f, t in K_MOVES if f == frm)
        hint = {"BLOCKED": f"`orc resume {rid}` after the user types `orc:resume {rid}`"}.get(frm)
        raise oc.OrcError(f"{rid}: K has no {frm} → {to}"
                          + (f" (allowed: {', '.join(allowed)})" if allowed else "")
                          + (f"; {hint}" if hint else ""))
    note = ""
    tasks = oc.load_registry(root, "task")
    kids = coders(root, rid, tasks)
    level = e.get("level")
    if to == "PLANNED":
        res = [a for a in child_arts(root, rid, "research") if a.get("status") != "rejected"]
        _need(res, f"{rid}: the request has no research ART yet (the understanding & design stage always stops)")
        for a in res:
            oc.ask_text(root, a["id"])  # sections still unfilled ⇒ OrcError
        _need([a for a in child_arts(root, rid, "plan") if a.get("status") == "accepted"],
              f"{rid}: no accepted plan yet (`orc gate` PROCEED, then `orc advance PLAN --to accepted`)")
        _need(kids, f"{rid}: no coder task yet (`orc new task --from PLAN`)")
    elif to == "READY_FOR_IMPLEMENTATION":
        _need(level in (0, 1), f"{rid}: level {level if level is not None else 'not set'} ⇒ only "
                               "APPROVAL_REQUIRED is allowed (READY_FOR_IMPLEMENTATION is only for L0/L1)")
    elif to == "IMPLEMENTING":
        import orc_approval  # noqa: PLC0415
        ok, why = orc_approval.check(root, rid)
        _need(ok, f"{rid}: APPROVAL_MISSING — {why}; the user types the `orc:approve ...` line, then `orc approve ...`")
        if level == 3:
            _need([d for d in oc.load_registry(root, "decision") if d.get("status") == "accepted"
                   and d.get("scope") in set(request_ids(root, rid))], f"{rid}: L3 needs an accepted DEC for the request")
    elif to == "VERIFYING":
        bad = [t["id"] for t in kids if t.get("status") not in ("in_review", "done")]
        _need(not bad, f"{rid}: coder tasks not submitted/blocked: {', '.join(bad)}")
    elif to == "DONE":
        bad = [t["id"] for t in kids if t.get("status") != "done"]
        _need(not bad, f"{rid}: coder tasks not done: {', '.join(bad)}")
    elif frm == "VERIFYING" and to == "IMPLEMENTING":
        _need([t for t in kids if t.get("status") != "done"], f"{rid}: every coder task is done — advance to DONE")
    entry = oc.set_fields(root, rid, {"status": to})
    stage = {"TRIAGED": 1, "PLANNED": 2, "VERIFYING": 3}.get(to)
    if stage:  # triage in shadow mode — recorded only, errors never block K
        import orc_triage  # noqa: PLC0415
        try:
            note = orc_triage.render(orc_triage.run(root, rid, stage))
        except Exception as exc:  # noqa: BLE001
            note = f"TRIAGE (shadow) error, ignored: {exc}"
    if to in ("APPROVAL_REQUIRED", "READY_FOR_IMPLEMENTATION"):
        import orc_approval  # noqa: PLC0415
        ids = [rid] + [a["id"] for a in child_arts(root, rid, "research") if a.get("status") == "draft"]
        scope = set(request_ids(root, rid))
        ids += [d["id"] for d in oc.load_registry(root, "decision")
                if d.get("status") == "proposed" and d.get("scope") in scope]
        note = orc_approval.request(root, ids)
    return entry, note


def child_dispatch_check(root: Path, t: dict) -> None:
    """A coder task of a K request is dispatched only while the root is IMPLEMENTING and its approval is still valid."""
    p = parent_of(root, t)
    if not is_root(p) or oc.agent_for(t.get("owner")) != "coder":
        return
    _need(p.get("status") == "IMPLEMENTING",
          f"{t['id']}: APPROVAL_MISSING — root task {p['id']} is {p.get('status')}, not IMPLEMENTING")
    import orc_approval  # noqa: PLC0415
    ok, why = orc_approval.check(root, p["id"])
    _need(ok, f"{t['id']}: APPROVAL_MISSING — {p['id']}: {why}")


# ---------------------------------------------------------------- verify gate before done

def _journal(root: Path) -> list:
    p = oc.journal_path(root)
    return [json.loads(l) for l in oc.read_text(p).splitlines() if l.strip()] if p.exists() else []


def verify_gate(root: Path, t: dict) -> tuple:
    """(failure_class | None, detail) from the latest `orc verify` **after** the latest submission. None yet ⇒ OrcError."""
    _need(oc.load_config(root).get("verify"), f"{t['id']}: a K project must have a `verify:` section in its config")
    rows = _journal(root)
    sub = max((i for i, r in enumerate(rows) if r.get("id") == t["id"] and r.get("to") == "in_review"), default=-1)
    ver = [r for r in rows[sub + 1:] if r.get("id") == t["id"] and r.get("event") == "verify"]
    _need(ver, f"{t['id']}: `orc verify {t['id']}` has not run since the latest submission — run it, then load the review result again")
    evd = {e["id"]: e for e in oc.load_registry(root, "evidence")}
    run = [evd[i] for i in ver[-1].get("evidence_ids", []) if i in evd]
    scope = [e for e in run if e.get("kind") == "scope"]
    if scope and scope[0].get("result") != "pass":
        return "SCOPE_VIOLATION", f"scope fail ({scope[0]['id']})"
    fails = [e for e in run if e.get("result") == "fail"]
    if fails:
        return VERIFY_FAILURE.get(fails[0].get("kind"), "TEST_FAILURE"), f"{fails[0]['id']}: {fails[0]['summary']}"
    env = [e for e in run if e.get("result") == "info"]
    if env:
        return "ENVIRONMENT_FAILURE", f"{env[0]['id']}: {env[0]['summary']}"
    return None, f"verify PASS ({', '.join(e['id'] for e in run)})"


def rollback_hint(root: Path, tid: str) -> str:
    """Commands the user runs to discard the task's changes (orc never resets on its own — deny_always)."""
    try:
        import orc_verify  # noqa: PLC0415
        cp = orc_verify.load_checkpoint(root, tid)
    except oc.OrcError:
        return "no checkpoint — inspect `git diff` and restore by hand"
    return "; ".join(f"git -C {rel} diff {s['head']}  (inspect) · git -C {rel} checkout {s['head']} -- <file to discard>"
                     for rel, s in cp["repos"].items())


def escalate(root: Path, t: dict, reason: str) -> None:
    """ESCALATE on a child task ⇒ root task BLOCKED (remembers the status for resume)."""
    p = parent_of(root, t)
    if is_root(p) and p.get("status") not in ("BLOCKED", *CLOSED):
        with oc.journal_context(extra={"reason": oc._short(reason, 200), "from_task": t["id"]}):
            oc.set_fields(root, p["id"], {"status": "BLOCKED", "resume_status": p["status"], "needs_user": True})


def review_route(root: Path, task: dict, env: dict) -> tuple:
    """Route the review result of a K request's coder task (I2). Returns (task changes, route, escalate?)."""
    level = (parent_of(root, task) or {}).get("level")
    attempts = int(task.get("attempts") or 0)
    fc = env.get("failure_class")
    if env["status"] == "done" and not fc:
        fc, detail = verify_gate(root, task)
        if fc is None:
            return {"status": "done", "failure_class": None, "recovery_action": None}, f"done — {detail}", False
    else:
        detail = env.get("summary", "")
    action = recovery(fc, level, attempts + 1 if fc != "ENVIRONMENT_FAILURE" else attempts)
    ch = {"failure_class": fc, "recovery_action": action}
    if action == "REPAIR":
        if fc == "SPEC_GAP":  # L0/L1: replan as in the basic workflow
            return None, "", False
        ch.update(status="ready", attempts=attempts + 1)
        return ch, f"REPAIR {fc}: back to coder (attempt {attempts + 1}) — {detail}", False
    if action == "ROLLBACK":
        ch.update(status="blocked", needs_user=True)
        return ch, f"ROLLBACK + ESCALATE {fc}: the user discards the changes — {rollback_hint(root, task['id'])}", True
    ch.update(status="blocked", needs_user=True)
    return ch, f"ESCALATE {fc}: ASK USER — {detail}", True


# ---------------------------------------------------------------- user commands: resume / abort

def _user_said(root: Path, line: str, after: str) -> bool:
    import orc_approval  # noqa: PLC0415
    want = " ".join(line.split()).lower()
    return any(r.get("ts", "") > after and any(" ".join(l.split()).lower() == want for l in r.get("orc") or [])
               for r in orc_approval.prompt_lines(root))


def _last_row_ts(root: Path, rid: str, to=None) -> str:
    rows = [r for r in _journal(root) if r.get("id") == rid and (to is None or r.get("to") == to)]
    return rows[-1]["ts"] if rows else ""


def resume(root: Path, rid: str) -> dict:
    _, _, e = oc.find_entry(root, rid)
    _need(is_root(e) and e.get("status") == "BLOCKED", f"{rid}: only a BLOCKED K root task can be resumed")
    _need(_user_said(root, f"orc:resume {rid}", _last_row_ts(root, rid, "BLOCKED")),
          f"{rid}: no `orc:resume {rid}` typed by the user since it was blocked")
    tasks = oc.load_registry(root, "task")
    for t in children(root, rid, tasks):
        if t.get("status") in ("blocked", "failed") and t.get("needs_user"):
            oc.set_fields(root, t["id"], {"status": "ready", "needs_user": False, "recovery_action": None})
    return oc.set_fields(root, rid, {"status": e.get("resume_status") or "IMPLEMENTING",
                                     "resume_status": None, "needs_user": False})


def abort(root: Path, rid: str) -> dict:
    """The user cancels an open task: K root task ⇒ ABORTED; normal/child task ⇒ aborted (not run, not shown
    in the brief). Only once the user typed `orc:abort ID` after that task's latest status change."""
    _, _, e = oc.find_entry(root, rid)
    _need(not is_closed(e.get("status")), f"{rid}: already closed ({e.get('status')}), cannot be aborted")
    _need(_user_said(root, f"orc:abort {rid}", _last_row_ts(root, rid)),
          f"{rid}: no `orc:abort {rid}` typed by the user since its latest status change")
    return oc.set_fields(root, rid, {"status": "ABORTED" if is_root(e) else "aborted", "needs_user": False})


def next_lines(root: Path, tasks: list) -> list:
    hint = {"NEW": "orc advance {id} --to TRIAGED", "TRIAGED": "orc advance {id} --to RESEARCHING",
            "RESEARCHING": "stage 1 (researcher) → plan accepted → orc advance {id} --to PLANNED",
            "PLANNED": "orc advance {id} --to APPROVAL_REQUIRED (prints the line the user must type)",
            "APPROVAL_REQUIRED": "wait for the user to type the orc:approve line → orc approve … → orc advance {id} --to IMPLEMENTING",
            "READY_FOR_IMPLEMENTATION": "wait for the user to confirm (orc:approve) → orc advance {id} --to IMPLEMENTING",
            "IMPLEMENTING": "dispatch coder tasks (READY below); all submitted → orc advance {id} --to VERIFYING",
            "VERIFYING": "every coder task done → orc advance {id} --to DONE",
            "BLOCKED": "ASK USER — the user types orc:resume {id}, then orc resume {id}"}
    return [f"K: {t['id']} [{t['status']}] → {hint[t['status']].format(id=t['id'])}"
            for t in tasks if is_root(t) and t.get("status") in hint]
