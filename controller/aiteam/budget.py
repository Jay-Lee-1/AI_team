"""실제 사용량 기록과 예산 적용. 값이 없으면 0이 아니라 '미수집'(None)으로 둔다."""
from datetime import datetime

from . import settings
from .db import local_day, now


def month_of(day):
    return day[:7]


def reserve(db, task, attempt, model, per_call, is_mock):
    """호출 시작 시 '집계 중' 행을 만든다. 측정 지연 동안 예약액을 예산에 포함한다."""
    day = local_day()
    db.x("""INSERT OR IGNORE INTO usage(task_id,attempt,project_id,dept,model,status,is_mock,
            reserved_usd,day,month,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
         (task["id"], attempt, task["project_id"], task["dept"], "(집계 중)",
          "mock" if is_mock else "collecting", int(is_mock), 0 if is_mock else per_call,
          day, month_of(day), now()))


def record(db, task, attempt, result):
    """호출 완료 후 실제 사용량으로 교체. 같은 결과(uuid)는 한 번만 기록한다."""
    with db.tx():
        db.x("DELETE FROM usage WHERE task_id=? AND attempt=? AND model='(집계 중)'",
             (task["id"], attempt))
        day = local_day()
        if result.is_mock:
            db.x("""INSERT OR IGNORE INTO usage(task_id,attempt,project_id,dept,model,status,is_mock,
                    day,month,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 (task["id"], attempt, task["project_id"], task["dept"], "모의 호출", "mock", 1,
                  day, month_of(day), now()))
            return
        if result.result_uuid and db.one("SELECT 1 FROM usage WHERE result_uuid=?",
                                         (result.result_uuid,)):
            return  # 중복 합산 방지
        if not result.usage:
            db.x("""INSERT OR IGNORE INTO usage(task_id,attempt,project_id,dept,model,status,
                    cost_usd,reserved_usd,session_id,result_uuid,day,month,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (task["id"], attempt, task["project_id"], task["dept"], "(미수집)", "missing",
                  result.cost_usd, 0 if result.cost_usd is not None else
                  settings.load()["budget"]["per_call_usd"],
                  result.session_id, result.result_uuid, day, month_of(day), now()))
            return
        for model, u in result.usage.items():
            db.x("""INSERT OR IGNORE INTO usage(task_id,attempt,project_id,dept,model,input_tokens,
                    output_tokens,cache_read,cache_write,cost_usd,status,session_id,result_uuid,
                    day,month,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (task["id"], attempt, task["project_id"], task["dept"], model, u.get("input"),
                  u.get("output"), u.get("cache_read"), u.get("cache_write"), u.get("cost"),
                  "collected", result.session_id, result.result_uuid, day, month_of(day), now()))


def totals(db, day=None, month=None):
    day = day or local_day()
    month = month or month_of(day)

    def agg(where, args):
        r = db.one(f"""SELECT SUM(COALESCE(cost_usd,0)) cost, SUM(reserved_usd) reserved,
               SUM(CASE WHEN status='collecting' THEN 1 ELSE 0 END) collecting,
               SUM(CASE WHEN status='missing' THEN 1 ELSE 0 END) missing,
               SUM(CASE WHEN status='collected' THEN 1 ELSE 0 END) collected,
               SUM(input_tokens) inp, SUM(output_tokens) outp, SUM(cache_read) cr,
               SUM(cache_write) cw, COUNT(DISTINCT task_id || '-' || attempt) calls
               FROM usage WHERE is_mock=0 AND {where}""", args)
        return {k: r[k] for k in r}
    return {"day": agg("day=?", (day,)), "month": agg("month=?", (month,)), "date": day}


def by_dept(db, day=None):
    day = day or local_day()
    rows = db.all("""SELECT dept, SUM(input_tokens) inp, SUM(output_tokens) outp,
        SUM(cache_read) cr, SUM(cache_write) cw, SUM(cost_usd) cost,
        SUM(CASE WHEN status='collecting' THEN 1 ELSE 0 END) collecting,
        SUM(CASE WHEN status='collected' THEN 1 ELSE 0 END) collected,
        SUM(is_mock) mock_calls FROM usage WHERE day=? GROUP BY dept""", (day,))
    return {r["dept"]: r for r in rows}


def gate(db, priority, is_mock):
    """새 유료 호출을 시작해도 되는지. ('ok'|'slow'|'stop', 이유)"""
    if is_mock:
        return "ok", ""
    s = settings.load()
    if not settings.is_paid_ready(s):
        return "stop", "인증 방식과 하루·월 예산을 먼저 설정해야 실제 호출을 시작합니다"
    b = s["budget"]
    t = totals(db)
    margin = 1 - (b.get("safety_margin_ratio") or 0)
    per_call = b.get("per_call_usd") or 0
    level, reason = "ok", ""
    for scope, limit in (("day", b.get("daily_usd")), ("month", b.get("monthly_usd"))):
        if limit is None:
            continue
        spent = (t[scope]["cost"] or 0) + (t[scope]["reserved"] or 0)
        name = "하루" if scope == "day" else "월"
        if spent + per_call > limit * margin:
            return "stop", f"{name} 예산 상한 도달(안전 여유분 포함): 새 유료 호출 중지"
        if spent >= limit * (b.get("slowdown_ratio") or 0.8):
            level, reason = "slow", f"{name} 예산 {int((b.get('slowdown_ratio') or 0.8)*100)}% 근접: 핵심 작업만 진행"
    if b.get("daily_tokens"):
        d = t["day"]
        used = sum((d[k] or 0) for k in ("inp", "outp", "cw"))
        if used >= b["daily_tokens"] * margin:
            return "stop", "하루 토큰 한도 도달: 새 유료 호출 중지"
    if level == "slow" and priority > 2:
        return "stop", reason
    return level, reason


def hour_now():
    return datetime.now().hour
