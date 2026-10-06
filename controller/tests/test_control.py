"""일시 중지·재개·긴급 중지, 사용자 답변 대기 중 다른 업무, 재시작 복구, 재시도."""
import time

from helpers import TeamCase

from aiteam import views
from aiteam.paths import P


class ControlTest(TeamCase):
    def test_global_pause_stops_new_assignment(self):
        self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        self.s.control("start")
        self.s.control("pause")
        self.run_ticks(5)
        self.assertEqual(self.task("planning.define_mvp")["status"], "queued")
        self.s.control("resume")
        self.run_ticks(20, until=lambda: self.task("planning.define_mvp")["status"] == "done")
        self.assertEqual(self.task("planning.define_mvp")["status"], "done")

    def test_department_and_project_pause(self):
        pid = self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        self.s.control("start")
        self.s.control("pause", "dept", "planning")
        self.run_ticks(5)
        self.assertEqual(self.task("planning.define_mvp")["status"], "queued")
        self.assertIn("부서 일시 중지", str(self.db.meta_get("blocked_reasons")))
        self.s.control("resume", "dept", "planning")
        self.s.control("pause", "project", pid)
        self.run_ticks(5)
        self.assertEqual(self.task("planning.define_mvp")["status"], "queued")
        self.s.control("resume", "project", pid)
        self.run_ticks(20, until=lambda: self.task("planning.define_mvp")["status"] == "done")

    def test_emergency_stop_marks_needs_verification_and_resume(self):
        self.mock.delay = 3
        self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        self.s.control("start")
        self.s.tick()
        time.sleep(0.2)
        self.assertEqual(self.task("planning.define_mvp")["status"], "running")
        st = views.state(self.db, self.s)
        card = next(c for c in st["departments"] if c["key"] == "planning")
        self.assertEqual(card["status"], "running")  # 화면 상태 = 실제 상태
        t0 = time.time()
        self.s.control("emergency_stop")
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual(self.task("planning.define_mvp")["status"], "needs_verification")
        self.assertEqual(self.s.team_state(), "emergency")
        self.run_ticks(3)
        self.assertEqual(self.task("planning.define_mvp")["status"], "needs_verification")
        self.mock.delay = 0.02
        self.s.control("resume")
        self.assertEqual(self.task("planning.define_mvp")["status"], "queued")
        self.run_ticks(20, until=lambda: self.task("planning.define_mvp")["status"] == "done")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM tasks")["n"], 1)  # 반복 생성 없음

    def test_restart_recovery_preserves_partial_changes(self):
        pid = self.demo_to_release()
        proj = self.wf.project(pid)
        ws = P.workspace(proj["slug"])
        # 개발 작업이 파일 일부를 쓴 상태에서 프로그램이 꺼진 상황을 만든다
        t = self.task("development.implement")
        self.db.update("tasks", t["id"], status="running")
        (ws / "partial.txt").write_text("중간까지 쓴 파일", encoding="utf-8")
        (P.journal / f"task-{t['id']}.json").write_text('{"paths": ["partial.txt"]}', encoding="utf-8")
        self.db.x("INSERT INTO usage(task_id,attempt,dept,model,status,reserved_usd,day,month) "
                  "VALUES(?,?,?,?,?,?,?,?)", (t["id"], 9, "development", "(집계 중)", "collecting", 0.5,
                                              "2026-01-01", "2026-01"))
        from aiteam import gitops
        from aiteam.scheduler import Scheduler
        s2 = Scheduler(self.db, mock_runner=self.mock)
        s2.recover_on_start()
        self.assertEqual(self.task("development.implement")["status"], "queued")
        self.assertNotIn("partial.txt", gitops.dirty_files(ws))  # 커밋으로 보존
        log = gitops._git(ws, "log", "--oneline")
        self.assertIn("검증 필요", log)
        self.assertEqual(self.db.one("SELECT status FROM usage WHERE attempt=9")["status"], "missing")
        # 완료된 작업은 다시 실행하지 않는다
        self.assertEqual(self.task("planning.define_mvp")["status"], "done")

    def test_other_work_while_waiting_for_user(self):
        self.settings({"decision_wait_hours": 0})
        pid = self.demo_to_release()
        self.wf.answer_decision(self.pending("deploy_approval")["id"], {"approve": True})
        self.wf.answer_decision(self.pending("user_action")["id"], {"done": True})
        # 사용자 결정 대기 항목(개선안 선택)을 만들고, 그동안 다른 업무가 진행되는지 확인
        from aiteam import inquiries
        proj = self.wf.project(pid)
        for i in range(3):
            inquiries.submit(self.db, proj, {"category": "feature", "content": f"수정 기능 요청 {i}", "anon_id": str(i)})
        self.run_ticks(60, until=lambda: self.pending("proposal_selection"))
        self.run_ticks(60, until=lambda: self.task("planning.strengthen_proposal") and
                       self.task("planning.strengthen_proposal")["status"] == "done")
        fb = self.db.all("SELECT kind,status FROM tasks WHERE is_fallback=1")
        self.assertTrue(any(f["kind"] == "planning.strengthen_proposal" for f in fb))
        # 결정은 대신 내리지 않는다
        self.assertEqual(self.pending("proposal_selection")["status"], "pending")
        self.assertEqual(self.db.one("SELECT status FROM proposals")["status"], "proposed")
        # 할 일이 끝나면 같은 일을 반복 생성하지 않는다
        self.run_ticks(40)
        n1 = self.db.one("SELECT COUNT(*) n FROM tasks")["n"]
        self.run_ticks(20)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM tasks")["n"], n1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM tasks WHERE kind='planning.strengthen_proposal'")["n"], 1)

    def test_same_cause_failure_retried_twice_then_reported(self):
        self.mock.fail_plan["planning.define_mvp"] = ["other"] * 5
        self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        self.s.control("start")
        for _ in range(5):
            self.db.x("UPDATE tasks SET retry_after=0")
            self.run_ticks(3)
        t = self.task("planning.define_mvp")
        self.assertEqual(t["status"], "failed")
        self.assertEqual(t["attempts"], 3)  # 최초 1회 + 재시도 2회
        self.assertTrue(self.db.one("SELECT 1 FROM events WHERE kind='error'"))
        st = views.state(self.db, self.s)
        self.assertEqual(next(c for c in st["departments"] if c["key"] == "planning")["status"], "error")

    def test_rate_limit_backoff(self):
        self.mock.fail_plan["planning.define_mvp"] = ["rate_limit"]
        self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        self.s.control("start")
        self.run_ticks(3)
        t = self.task("planning.define_mvp")
        self.assertEqual(t["status"], "queued")
        self.assertGreater(t["retry_after"], time.time() + 50)
        self.assertEqual(t["same_cause_count"], 0)
