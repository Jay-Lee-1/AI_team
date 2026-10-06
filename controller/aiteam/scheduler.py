"""상시 실행 루프. 실행 가능한 일이 있을 때만 모델을 호출하고, 없으면 토큰을 쓰지 않고 기다린다."""
import hashlib
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import budget, checks, contracts, gitops, logs, runner, settings
from .db import jd, jl, now
from .paths import P
from .workflow import WRITE_KINDS, ApplyError, GateError, Workflow, _key

log = logs.setup()

STATE_KO = {"stopped": "정지", "running": "실행 중", "paused": "일시 중지", "emergency": "긴급 중지"}


class Scheduler:
    def __init__(self, db, mock_runner=None, real_runner=None, tick_sec=2.0):
        self.db = db
        self.wf = Workflow(db)
        self.mock = mock_runner or runner.MockRunner()
        self.real = real_runner or runner.ClaudeCliRunner()
        self.tick_sec = tick_sec
        self.lock = threading.RLock()
        self.running = {}        # task_id -> (thread, cancel_event)
        self.write_locks = set()  # project_id (같은 파일 동시 수정 방지)
        self.stop_evt = threading.Event()
        self.last_house = 0
        self.last_cleanup = 0
        self.thread = None

    # ───────── 상태 ─────────
    def team_state(self):
        return self.db.meta_get("team_state", "stopped")

    def runner_for(self, project):
        if project["is_demo"] or settings.load()["runner"] == "mock":
            return self.mock
        return self.real

    def is_mock(self, project):
        return self.runner_for(project) is self.mock

    # ───────── 제어 ─────────
    def control(self, action, scope="global", target=None):
        if scope == "dept":
            if target not in ("planning", "design", "development", "qa", "release"):
                raise ValueError("알 수 없는 부서")
            paused = 1 if action == "pause" else 0
            self.db.x("INSERT INTO dept_state(dept,paused) VALUES(?,?) ON CONFLICT(dept) DO "
                      "UPDATE SET paused=excluded.paused", (target, paused))
            self.db.event("control", f"부서 {'일시 중지' if paused else '재개'}", dept=target)
            return
        if scope == "project":
            self.db.update("projects", int(target), paused=1 if action == "pause" else 0)
            self.db.event("control", f"프로젝트 {'일시 중지' if action == 'pause' else '재개'}",
                          project_id=int(target))
            return
        prev = self.team_state()
        if action in ("start", "resume"):
            if prev == "emergency":
                self._resume_after_emergency()
            self.db.meta_set("team_state", "running")
        elif action == "pause":
            self.db.meta_set("team_state", "paused")
        elif action == "emergency_stop":
            self.db.meta_set("team_state", "emergency")
            self._emergency()
        else:
            raise ValueError("알 수 없는 동작")
        self.db.event("control", f"전체 {STATE_KO[self.team_state()]} (이전: {STATE_KO.get(prev, prev)})")

    def _emergency(self):
        with self.lock:
            items = list(self.running.items())
        for tid, (_, cancel) in items:
            cancel.set()
            self.db.x("UPDATE tasks SET status='needs_verification', note=? WHERE id=?",
                      ("긴급 중지: 부분 변경 보존, 검증 필요", tid))
        runner.kill_all()
        checks.kill_all()
        for tid, (th, _) in items:
            th.join(timeout=30)
        # 중단 직후 부분 변경을 커밋으로 보존
        for t in self.db.all("SELECT * FROM tasks WHERE status='needs_verification'"):
            self._preserve_partial(t, "긴급 중지")

    def _resume_after_emergency(self):
        for t in self.db.all("SELECT * FROM tasks WHERE status='needs_verification'"):
            self._preserve_partial(t, "재개 전 확인")
            self.db.x("UPDATE tasks SET status='queued', note=? WHERE id=?",
                      ("긴급 중지 후 재개: 실제 파일 상태 확인 후 다시 진행", t["id"]))
            self.db.event("recover", f"검증 필요 작업 재개: {t['title']}", project_id=t["project_id"],
                          dept=t["dept"], task_id=t["id"])

    def _preserve_partial(self, task, why):
        journal = P.journal / f"task-{task['id']}.json"
        if not journal.exists():
            return
        proj = self.wf.project(task["project_id"])
        ws = P.workspace(proj["slug"])
        paths = jl(journal.read_text(encoding="utf-8"), {}).get("paths", [])
        dirty = set(gitops.dirty_files(ws))
        keep = [p for p in paths if p in dirty]
        if keep:
            gitops.commit_paths(ws, keep, f"[검증 필요] task {task['id']} 부분 변경 보존 ({why})")
            self.db.event("recover", f"부분 변경 {len(keep)}개 파일을 커밋으로 보존(검증 필요)",
                          project_id=task["project_id"], dept=task["dept"], task_id=task["id"])
        journal.unlink(missing_ok=True)

    def recover_on_start(self):
        """재시작 후 작업 상태 복구. 완료된 일은 다시 하지 않는다."""
        for t in self.db.all("SELECT * FROM tasks WHERE status='running'"):
            self._preserve_partial(t, "재시작 복구")
            self.db.x("UPDATE tasks SET status='queued', note=? WHERE id=?",
                      ("재시작 후 복구: 다시 진행", t["id"]))
            self.db.event("recover", f"재시작 후 복구: {t['title']}", project_id=t["project_id"],
                          dept=t["dept"], task_id=t["id"])
        # 결과를 받지 못한 호출은 '미수집'으로 남기고(0 아님) 예약액은 보수적으로 유지
        self.db.x("UPDATE usage SET status='missing', model='(미수집: 중단됨)' "
                  "WHERE status='collecting'")

    # ───────── 루프 ─────────
    def start_thread(self):
        self.recover_on_start()
        self.thread = threading.Thread(target=self.loop, name="scheduler", daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stop_evt.is_set():
            try:
                self.tick()
            except Exception:
                log.error("스케줄러 오류: %s", traceback.format_exc())
            self.stop_evt.wait(self.tick_sec)

    def shutdown(self, wait=30):
        self.stop_evt.set()
        with self.lock:
            threads = [th for th, _ in self.running.values()]
        for th in threads:
            th.join(timeout=wait)

    def tick(self):
        s = settings.load()
        if time.time() - self.last_cleanup > 86400:
            self.last_cleanup = time.time()
            logs.cleanup(s["log_retention_days"])
        if self.team_state() != "running":
            return
        projects = self.db.all("SELECT * FROM projects WHERE paused=0")
        if time.time() - self.last_house > 10:
            self.last_house = time.time()
            for p in projects:
                try:
                    self.wf.housekeeping(p["id"])
                except Exception:
                    log.error("정리 작업 오류: %s", traceback.format_exc())
        started = self.start_ready(s)
        with self.lock:
            busy = {t["project_id"] for t in self.db.all(
                "SELECT project_id FROM tasks WHERE status='running'")}
        if not started:
            for p in projects:
                if p["id"] in busy:
                    continue
                if self._has_runnable(p["id"]):
                    continue
                tid = self.wf.fallback_work(p["id"])
                if tid:
                    self.db.event("fallback", "사용자 결정 대기 중: 다른 실행 가능한 업무를 선택",
                                  project_id=p["id"], task_id=tid, is_mock=self.is_mock(p))

    def _has_runnable(self, pid):
        return bool(self.db.one("SELECT 1 FROM tasks WHERE project_id=? AND status IN "
                                "('queued','running')", (pid,)))

    def blocked_reason(self, task, project, s):
        if project["paused"]:
            return "프로젝트 일시 중지"
        d = self.db.one("SELECT paused FROM dept_state WHERE dept=?", (task["dept"],))
        if d and d["paused"]:
            return "부서 일시 중지"
        if task["kind"] in WRITE_KINDS and project["id"] in self.write_locks:
            return "같은 작업 사본을 다른 작업이 수정 중"
        if task["kind"] == "development.implement":
            cyc = self.wf.cycle(task["cycle_id"])
            if not cyc or not cyc["approved"]:
                return "사용자 선택 전 개발 차단"
            if not gitops.available():
                return "Git이 없어 복구 지점을 만들 수 없음(설치 필요)"
        if not self.is_mock(project):
            hold = self.db.meta_get("paid_hold_until")
            if hold and hold > time.time():
                return "사용 한도 소진: 재시도 대기"
            if self.db.meta_get("auth_problem"):
                return "인증 문제: 다시 로그인 필요"
            level, reason = budget.gate(self.db, task["priority"], False)
            if level == "stop":
                return reason
        return None

    def start_ready(self, s):
        started = 0
        with self.lock:
            cap = s["max_parallel_calls"] - len(self.running)
        if cap <= 0:
            return 0
        rows = self.db.all("SELECT * FROM tasks WHERE status='queued' AND retry_after<=? "
                           "ORDER BY priority, id", (time.time(),))
        reasons = {}
        for t in rows:
            if started >= cap:
                break
            proj = self.wf.project(t["project_id"])
            why = self.blocked_reason(t, proj, s)
            if why:
                reasons[t["id"]] = why
                continue
            self._start(t, proj)
            started += 1
        self.db.meta_set("blocked_reasons", reasons)
        return started

    def _start(self, task, proj):
        cancel = threading.Event()
        attempt = task["attempts"] + 1
        self.db.update("tasks", task["id"], status="running", started_at=now(), attempts=attempt)
        if task["kind"] in WRITE_KINDS:
            self.write_locks.add(proj["id"])
        th = threading.Thread(target=self._run, args=(task["id"], attempt, cancel),
                              name=f"task-{task['id']}", daemon=True)
        with self.lock:
            self.running[task["id"]] = (th, cancel)
        self.db.event("start", f"작업 시작: {task['title']}" + (" [모의 호출]" if self.is_mock(proj) else ""),
                      project_id=proj["id"], dept=task["dept"], task_id=task["id"],
                      is_mock=self.is_mock(proj))
        th.start()

    def _run(self, task_id, attempt, cancel):
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        proj = self.wf.project(task["project_id"])
        try:
            if task["kind"] == "qa.verify":
                results, findings = self.wf.qa_precheck(task)
                if cancel.is_set():
                    return
                if findings:
                    summary = self.wf.finish_qa(task, "fail", "코드 검증 단계에서 실패(모델 호출 생략)",
                                                findings, [], [], results)
                    return self._done(task, summary, {"findings": findings})
                task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
            spec = self.wf.build_spec(task)
            spec["cancel"] = cancel
            r = self.runner_for(proj)
            budget.reserve(self.db, task, attempt, spec["model"],
                           settings.load()["budget"]["per_call_usd"] or 0, r is self.mock)
            res = r.run(spec)
            budget.record(self.db, task, attempt, res)
            if res.raw:
                logs.save_call(task_id, attempt, res.raw)
            if cancel.is_set():
                return  # 긴급 중지: 상태는 needs_verification으로 이미 표시됨
            if not res.ok:
                return self._error(task, res.error_kind, res.error)
            self._check_token_limit(task, res)
            out = contracts.check(task["kind"], res.output)
            if self.db.one("SELECT status FROM tasks WHERE id=?", (task_id,))["status"] != "running":
                return
            task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
            summary = self.wf.apply(task, out)
            self._done(task, summary, out)
        except GateError as e:
            self.db.update("tasks", task_id, status="cancelled", note=str(e), finished_at=now())
            self.db.event("blocked", str(e), project_id=task["project_id"], dept=task["dept"],
                          task_id=task_id)
        except (ApplyError, contracts.ContractError) as e:
            self._error(task, "apply", str(e))
        except Exception as e:
            log.error("작업 %s 오류: %s", task_id, traceback.format_exc())
            self._error(task, "other", f"{type(e).__name__}: {e}")
        finally:
            with self.lock:
                self.running.pop(task_id, None)
                if task["kind"] in WRITE_KINDS:
                    self.write_locks.discard(task["project_id"])

    def _check_token_limit(self, task, res):
        if not task["token_limit"] or not res.usage:
            return
        used = sum((u.get("input") or 0) + (u.get("output") or 0) + (u.get("cache_write") or 0)
                   for u in res.usage.values())
        if used > task["token_limit"]:
            self.db.event("warn", f"토큰 한도 초과 사용: {used:,} > {task['token_limit']:,}",
                          project_id=task["project_id"], dept=task["dept"], task_id=task["id"])

    def _done(self, task, summary, out):
        slim = {k: v for k, v in (out or {}).items() if k not in ("changes", "preview_html",
                                                                 "direction_options")}
        self.db.update("tasks", task["id"], status="done", finished_at=now(),
                       result_summary=str(summary)[:500], output_json=jd(slim)[:20000],
                       last_error=None)
        self.db.event("done", f"완료: {task['title']} — {summary}", project_id=task["project_id"],
                      dept=task["dept"], task_id=task["id"],
                      is_mock=self.is_mock(self.wf.project(task["project_id"])))

    def _error(self, task, kind, msg):
        msg = (msg or "")[:1500]
        tid = task["id"]
        t = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if t["status"] == "needs_verification":
            return
        if kind == "killed":
            self.db.update("tasks", tid, status="needs_verification", note="중단됨: 검증 필요")
            return
        if kind == "rate_limit":
            n = t["backoff_count"] + 1
            if n > 8:
                return self._fail(t, "호출 제한이 계속되어 중지", msg)
            wait = min(60 * 2 ** (n - 1), 1800)
            self.db.update("tasks", tid, status="queued", backoff_count=n,
                           retry_after=time.time() + wait, last_error=msg)
            self.db.event("backoff", f"호출 제한: {wait}초 후 재시도", project_id=t["project_id"],
                          dept=t["dept"], task_id=tid)
            return
        if kind == "usage_limit":
            self.db.meta_set("paid_hold_until", time.time() + 1800)
            self.db.update("tasks", tid, status="queued", retry_after=time.time() + 1800,
                           last_error=msg)
            self.wf.add_decision(t["project_id"], "user_action", "Claude 사용 한도 소진 확인", {
                "todo": "Claude 사용 한도 확인", "why_now": "사용 한도가 소진되어 유료 호출을 멈췄습니다",
                "where": "claude.ai 설정 > 사용량 또는 Anthropic Console > Usage",
                "steps": ["한도 재설정 시각 확인", "필요하면 기다리거나 요금제 확인"],
                "cost": {"value": "요금제에 따라 다름", "kind": "unknown"},
                "report_back": "확인했다면 '완료'", "blocked_work": "모든 실제 모델 호출",
                "meanwhile": "모델 없이 할 수 있는 집계·문의 접수는 계속"},
                _key("usage-limit", datetime.now().strftime("%Y-%m-%d")))
            return
        if kind == "auth":
            self.db.meta_set("auth_problem", msg[:300])
            self.db.update("tasks", tid, status="queued", last_error=msg)
            self.wf.add_decision(t["project_id"], "user_action", "Claude 다시 로그인", {
                "todo": "Claude Code 인증 다시 하기", "why_now": "인증이 만료되었거나 없습니다",
                "where": "명령 프롬프트(cmd)", "steps": ["cmd 창에서 claude 실행", "/login 입력 후 안내대로 로그인",
                                                   "API 키 방식이면 환경 변수 ANTHROPIC_API_KEY 확인(키는 채팅에 붙여 넣지 않기)",
                                                   "관리 화면에서 '완료' 누르기"],
                "cost": {"value": "없음", "kind": "confirmed"}, "report_back": "로그인 완료 여부만",
                "blocked_work": "모든 실제 모델 호출", "meanwhile": "모의 데모·문의 접수는 계속"},
                _key("auth", msg[:40]))
            return
        sig = hashlib.sha256(f"{kind}:{msg[:120]}".encode()).hexdigest()[:16]
        same = t["same_cause_count"] + 1 if sig == t["last_error_sig"] else 1
        if same > 2:
            return self._fail(t, "같은 원인으로 2회 재시도 후에도 실패", msg)
        self.db.update("tasks", tid, status="queued", same_cause_count=same, last_error_sig=sig,
                       last_error=msg, retry_after=time.time() + 15 * same)
        self.db.event("retry", f"오류 후 재시도 예정({same}/2): {msg[:200]}",
                      project_id=t["project_id"], dept=t["dept"], task_id=tid)

    def _fail(self, t, why, msg):
        self.db.update("tasks", t["id"], status="failed", finished_at=now(), last_error=msg,
                       note=why)
        self.db.event("error", f"{why}: {t['title']} — {msg[:300]}", project_id=t["project_id"],
                      dept=t["dept"], task_id=t["id"])

    def retry_task(self, tid):
        t = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t or t["status"] not in ("failed", "needs_verification"):
            raise ValueError("다시 시도할 수 없는 작업입니다")
        if self.db.one("SELECT 1 FROM tasks WHERE dedup_key=? AND id!=? AND status NOT IN "
                       "('failed','cancelled')", (t["dedup_key"], tid)):
            raise ValueError("같은 작업이 이미 대기 중입니다")
        self.db.update("tasks", tid, status="queued", same_cause_count=0, backoff_count=0,
                       retry_after=0, note="사용자가 다시 시도")
        self.db.event("control", f"사용자 요청으로 다시 시도: {t['title']}",
                      project_id=t["project_id"], dept=t["dept"], task_id=tid)
