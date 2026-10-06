"""부서 간 전달, 선택 전 개발 차단, 중복 방지, 개선 순환."""
from helpers import TeamCase

from aiteam import inquiries
from aiteam.workflow import GateError


class FlowTest(TeamCase):
    def test_initial_handoff_through_five_departments(self):
        pid = self.demo_to_release()
        kinds = [t["kind"] for t in self.db.all("SELECT kind FROM tasks WHERE status='done' ORDER BY id")]
        self.assertEqual(kinds, ["planning.define_mvp", "design.initial", "development.implement",
                                 "qa.verify", "release.prepare"])
        handoffs = [e["message"] for e in self.db.all("SELECT message FROM events WHERE kind='handoff'")]
        self.assertTrue(any("기획·고객 인사이트 → 디자인" in h for h in handoffs))
        self.assertTrue(any("품질 검증 → 출시·운영" in h for h in handoffs))
        qa = self.db.one("SELECT * FROM qa_results")
        self.assertEqual(qa["verdict"], "pass")
        self.assertIn("통과", qa["evidence"])  # 실제 실행된 검증 명령 증거
        # 검증되지 않은 홍보 문구는 제외
        dep = self.pending("deploy_approval")
        self.assertIn("AI 자동 일정 추천", dep["body_json"])
        self.assertNotIn("AI 자동 일정 추천", self.db.one(
            "SELECT body_json FROM decisions WHERE id=?", (dep["id"],))["body_json"].split("dropped_claims")[0])
        # 배포 승인 전에는 배포 완료가 아님
        self.assertEqual(self.wf.project(pid)["stage"], "배포 승인 대기")

    def test_no_development_before_selection(self):
        pid = self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        cyc = self.db.one("SELECT * FROM cycles WHERE project_id=?", (pid,))
        with self.assertRaises(GateError):
            self.wf.create_task(pid, "development.implement", "몰래 개발", {}, cycle_id=cyc["id"])
        self.assertIsNone(self.task("development.implement"))
        self.assertTrue(self.db.one("SELECT 1 FROM events WHERE kind='blocked'"))

    def test_duplicate_task_prevented(self):
        pid = self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        a = self.wf.create_task(pid, "release.docs", "안내", {"x": 1}, code_version="abc")
        b = self.wf.create_task(pid, "release.docs", "안내", {"x": 1}, code_version="abc")
        c = self.wf.create_task(pid, "release.docs", "안내", {"x": 1}, code_version="def")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_improvement_cycle_requires_user_choice(self):
        pid = self.demo_to_release()
        self.wf.answer_decision(self.pending("deploy_approval")["id"], {"approve": True})
        self.wf.answer_decision(self.pending("user_action")["id"], {"done": True})
        proj = self.wf.project(pid)
        self.assertEqual(proj["stage"], "운영 중")
        for i, t in enumerate(["수정이 안 돼요", "오타 수정 기능 주세요", "고칠 수가 없어요"]):
            inquiries.submit(self.db, proj, {"category": "feature", "content": t, "anon_id": f"c{i}"})
        self.run_ticks(60, until=lambda: self.pending("proposal_selection"))
        prop = self.db.one("SELECT * FROM proposals")
        self.assertEqual(prop["status"], "proposed")
        # 선택 전에는 개선 디자인·개발이 없다
        self.run_ticks(10)
        self.assertIsNone(self.task("design.improvement"))
        self.wf.decide_proposal(prop["id"], "select", "수정 버튼만")
        self.run_ticks(80, until=lambda: self.pending("deploy_approval"))
        self.assertEqual(self.task("development.implement")["status"], "done")
        self.wf.answer_decision(self.pending("deploy_approval")["id"], {"approve": True})
        self.wf.answer_decision(self.pending("user_action")["id"], {"done": True})
        c = self.db.one("SELECT * FROM clusters")
        self.assertEqual(c["status"], "deployed")  # 배포 완료 ≠ 해결 확인
        self.wf.confirm_resolved(c["id"])
        self.assertEqual(self.db.one("SELECT status FROM clusters")["status"], "resolved_confirmed")
        stats = inquiries.cluster_stats(self.db, c["id"])
        self.assertEqual((stats["count"], stats["customers"]), (3, 3))

    def test_rejected_and_held_proposals_not_developed(self):
        pid = self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        for st in ("proposed", "proposed"):
            self.db.insert("proposals", project_id=pid, batch_task=0, title="x", body_json="{}",
                           size="small", status=st, cluster_ids="[]", inquiry_ids="[]")
        ids = [r["id"] for r in self.db.all("SELECT id FROM proposals")]
        self.wf.decide_proposal(ids[0], "reject")
        self.wf.decide_proposal(ids[1], "hold")
        self.assertIsNone(self.task("design.improvement"))
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM cycles WHERE kind='improvement'")["n"], 0)


class UserActionFilterTest(TeamCase):
    def test_review_requests_not_shown_as_user_tasks(self):
        pid = self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        task = {"id": 1, "project_id": pid, "dept": "design", "cycle_id": None}
        ua = {"todo": "설계 검토 및 승인", "why_now": "", "where": "", "steps": [],
              "cost": {"value": "", "kind": "unknown"}, "report_back": "", "blocked_work": "", "meanwhile": ""}
        self.wf.add_user_actions(task, [ua, ua | {"todo": "결제 서비스 가입과 본인 인증"}])
        titles = [d["title"] for d in self.db.all("SELECT title FROM decisions")]
        self.assertEqual(titles, ["결제 서비스 가입과 본인 인증"])
