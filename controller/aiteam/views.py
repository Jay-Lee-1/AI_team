"""관리 화면에 보낼 상태. 화면 갱신에는 모델을 호출하지 않는다."""
import os
import shutil
from datetime import datetime, timezone

from . import budget, departments, inquiries, settings
from .db import jl
from .paths import P
from .scheduler import STATE_KO
from .workflow import DECISION_DEPT

DEPT_STATUS_KO = {"idle": "대기", "running": "실행 중", "waiting_user": "사용자 답변 대기",
                  "paused": "일시 중지", "error": "오류"}


def disk_info():
    try:
        u = shutil.disk_usage(P.root)
    except OSError:
        return {"path": str(P.root), "free_gb": None, "total_gb": None, "warning": "확인 불가"}
    drive = os.path.splitdrive(str(P.root))[0] or "/"
    s = settings.load()
    free = round(u.free / 1024 ** 3, 1)
    warn = []
    if os.name == "nt" and drive.upper() != s["required_drive"].upper():
        warn.append(f"AI팀 폴더가 {s['required_drive']} 드라이브가 아닙니다")
    if free < s["min_free_gb"]:
        warn.append(f"여유 공간이 {s['min_free_gb']}GB보다 적습니다")
    return {"path": str(P.root), "drive": drive, "free_gb": free,
            "total_gb": round(u.total / 1024 ** 3, 1), "warning": " / ".join(warn)}


def _age_h(ts):
    try:
        return round((datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def state(db, sched, project_id=None):
    s = settings.load()
    projects = db.all("SELECT id,slug,name,goal,stage,paused,is_demo,app_version FROM projects ORDER BY id")
    if project_id is None and projects:
        project_id = projects[-1]["id"]
    proj = next((p for p in projects if p["id"] == project_id), None)
    pid = proj["id"] if proj else -1
    t = budget.totals(db)
    gate_level, gate_reason = budget.gate(db, 1, False)
    team_state = sched.team_state()

    # 부서 카드
    by = budget.by_dept(db)
    pending = db.all("SELECT * FROM decisions WHERE status='pending' ORDER BY id")
    cards = []
    for key in departments.ORDER:
        cfg = departments.config(key)
        run = db.one("SELECT * FROM tasks WHERE dept=? AND status='running' ORDER BY id LIMIT 1", (key,))
        nxt = db.one("SELECT * FROM tasks WHERE dept=? AND status='queued' ORDER BY priority,id LIMIT 1", (key,))
        last = db.one("SELECT * FROM tasks WHERE dept=? AND status IN ('done','failed','cancelled',"
                      "'needs_verification') ORDER BY COALESCE(finished_at,created_at) DESC, id DESC LIMIT 1", (key,))
        dpaused = db.one("SELECT paused FROM dept_state WHERE dept=?", (key,))
        dpaused = bool(dpaused and dpaused["paused"])
        waiting = [d for d in pending if DECISION_DEPT.get(d["kind"]) == key]
        if run:
            st = "running"
        elif last and last["status"] in ("failed", "needs_verification"):
            st = "error"
        elif waiting:
            st = "waiting_user"
        elif dpaused or team_state in ("paused", "emergency"):
            st = "paused"
        else:
            st = "idle"
        u = by.get(key) or {}
        mock_run = False
        if run:
            rp = db.one("SELECT is_demo FROM projects WHERE id=?", (run["project_id"],))
            mock_run = bool(rp and rp["is_demo"]) or s["runner"] == "mock"
        cards.append({
            "key": key, "name": cfg["name"], "order": cfg["order"],
            "next": departments.NAMES.get(cfg["next"]), "status": st,
            "status_ko": DEPT_STATUS_KO[st], "paused": dpaused, "mock": mock_run,
            "current": (run or nxt or {}).get("title"),
            "current_kind": "실행 중" if run else ("대기열" if nxt else None),
            "waiting": [w["title"] for w in waiting][:3],
            "recent": last and {"title": last["title"], "status": last["status"],
                                "summary": last["result_summary"] or last["note"] or last["last_error"]},
            "usage": {"input": u.get("inp"), "output": u.get("outp"), "cache_read": u.get("cr"),
                      "cache_write": u.get("cw"), "cost": u.get("cost"),
                      "collecting": bool(u.get("collecting")), "collected": u.get("collected") or 0,
                      "mock_calls": u.get("mock_calls") or 0},
            "permissions": cfg["permissions"],
        })

    handoffs = db.all("SELECT ts, dept, message FROM events WHERE kind='handoff' ORDER BY id DESC LIMIT 10")
    for h in handoffs:
        h["age_s"] = (_age_h(h["ts"]) or 0) * 3600

    decisions = []
    for d in pending:
        if proj and d["project_id"] not in (pid, None):
            continue
        body = jl(d["body_json"], {})
        decisions.append({"id": d["id"], "kind": d["kind"], "title": d["title"], "body": body,
                          "requested_at": d["requested_at"], "age_h": _age_h(d["requested_at"]),
                          "blocks": d["blocks"],
                          "long_wait": (_age_h(d["requested_at"]) or 0) >= s["decision_wait_hours"]})

    clusters = []
    for c in db.all("SELECT * FROM clusters WHERE project_id=? ORDER BY id DESC", (pid,)):
        st = inquiries.cluster_stats(db, c["id"])
        clusters.append({**{k: c[k] for k in ("id", "title", "problem", "requested_feature",
                                                "underlying_problem", "kind", "severity", "impact",
                                                "status", "reply_draft")},
                         "facts": jl(c["facts_json"], []), "estimates": jl(c["estimates_json"], []),
                         "quotes": jl(c["quotes_json"], []), "stats": st,
                         "inquiry_ids": [r["receipt_no"] for r in db.all(
                             "SELECT receipt_no FROM inquiries WHERE cluster_id=?", (c["id"],))]})
    inq = db.all("SELECT id,receipt_no,category,content,expected,app_version,screen,device,"
                 "CASE WHEN contact!='' THEN '있음' ELSE '' END contact,attachments_json,"
                 "repeat_of_customer,status,cluster_id,source,created_at FROM inquiries "
                 "WHERE project_id=? ORDER BY id DESC LIMIT 60", (pid,))
    for i in inq:
        i["category_ko"] = inquiries.CATEGORIES.get(i["category"], i["category"])
        i["status_ko"] = inquiries.STATUS_KO.get(i["status"], i["status"])
        i["attachments"] = jl(i.pop("attachments_json"), [])

    proposals = []
    for p in db.all("SELECT * FROM proposals WHERE project_id=? ORDER BY id DESC LIMIT 30", (pid,)):
        cids = jl(p["cluster_ids"], [])
        ev = [inquiries.cluster_stats(db, c) | {"cluster_id": c} for c in cids]
        proposals.append({"id": p["id"], "title": p["title"], "size": p["size"], "status": p["status"],
                          "scope_note": p["scope_note"], "body": jl(p["body_json"], {}),
                          "evidence": ev, "inquiry_count": len(jl(p["inquiry_ids"], [])),
                          "decided_at": p["decided_at"], "created_at": p["created_at"]})

    arts = db.all("SELECT id,task_id,dept,kind,path,title,created_at FROM artifacts WHERE project_id=? "
                  "ORDER BY id DESC LIMIT 40", (pid,))
    qa = db.all("SELECT * FROM qa_results WHERE project_id=? ORDER BY id DESC LIMIT 10", (pid,))
    for q in qa:
        q["checks"] = jl(q.pop("checks_json"), [])
        q["findings"] = jl(q.pop("findings_json"), [])

    tasks = db.all("SELECT id,project_id,dept,kind,title,status,priority,is_fallback,attempts,note,"
                   "last_error,result_summary,retry_after,created_at,started_at,finished_at,token_limit,"
                   "time_limit_sec,done_criteria FROM tasks WHERE project_id=? ORDER BY id DESC LIMIT 60", (pid,))
    blocked = sched.db.meta_get("blocked_reasons", {}) or {}
    for tk in tasks:
        tk["blocked_reason"] = blocked.get(str(tk["id"])) or blocked.get(tk["id"])
    timeline = db.all("SELECT * FROM events WHERE project_id=? OR project_id IS NULL ORDER BY id DESC LIMIT 120",
                      (pid,))
    releases = db.all("SELECT * FROM releases WHERE project_id=? ORDER BY id DESC LIMIT 10", (pid,))
    psettings = jl(db.one("SELECT settings_json FROM projects WHERE id=?", (pid,))["settings_json"], {}) if proj else {}

    return {
        "now": datetime.now().astimezone().isoformat(timespec="seconds"),
        "team": {"state": team_state, "state_ko": STATE_KO.get(team_state, team_state),
                 "runner": s["runner"], "paid_ready": settings.is_paid_ready(s),
                 "auth_mode": s["auth"].get("mode"),
                 "api_key_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
                 "budget": s["budget"], "models": s["models"], "usage": t,
                 "gate": {"level": gate_level, "reason": gate_reason},
                 "paid_hold": bool((db.meta_get("paid_hold_until") or 0) > datetime.now().timestamp()),
                 "auth_problem": db.meta_get("auth_problem"),
                 "decision_wait_hours": s["decision_wait_hours"],
                 "auto_bugfix": s["maintenance"]["auto_bugfix"],
                 "reply_auto_send": s["customer_reply_auto_send"],
                 "ports": s["ports"], "patent_prep": s["optional_features"]["patent_prep"]},
        "disk": disk_info(),
        "projects": projects, "project": proj, "project_settings": psettings,
        "departments": cards, "handoffs": handoffs,
        "decisions": decisions, "clusters": clusters, "inquiries": inq, "proposals": proposals,
        "artifacts": arts, "qa": qa, "tasks": tasks, "timeline": timeline, "releases": releases,
        "allowlist": s["check_command_allowlist"],
    }
