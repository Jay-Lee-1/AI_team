"""허용 경로 밖 쓰기 차단, 명령 제한, 비밀정보 가림, 검증 기준 유지."""
import os

from helpers import TeamCase

from aiteam import gitops
from aiteam.paths import P
from aiteam.safety import SafetyError, guarded_write, redact, safe_join, validate_check_command
from aiteam.workflow import ApplyError


class SafetyTest(TeamCase):
    def test_path_escape_blocked(self):
        base = P.projects / "x"
        base.mkdir()
        for bad in ["../evil.txt", "/etc/passwd", "C:/Windows/x", "a/../../b", ".git/config",
                    "CON.txt", "a:stream", "sub/.env"]:
            with self.assertRaises(SafetyError, msg=bad):
                safe_join(base, bad)
        self.assertTrue(str(safe_join(base, "src/app.js")).endswith(os.path.join("src", "app.js")))

    def test_symlink_escape_blocked(self):
        base = P.projects / "x"
        base.mkdir()
        outside = P.root / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, base / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("이 환경에서는 링크를 만들 수 없음")
        with self.assertRaises(SafetyError):
            guarded_write(base, "link/x.txt", "x", [P.projects])
        self.assertFalse((outside / "x.txt").exists())

    def test_write_outside_allowed_roots_blocked(self):
        with self.assertRaises(SafetyError):
            guarded_write(P.state, "settings.json", "{}", [P.projects])

    def test_dev_output_with_escape_path_rejected(self):
        pid = self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        cyc = self.db.one("SELECT * FROM cycles")
        self.db.update("cycles", cyc["id"], approved=1)
        task = {"id": 99, "project_id": pid, "cycle_id": cyc["id"], "dept": "development"}
        out = {"summary": "x", "changes": [{"path": "../../state/settings.json", "op": "create",
                                            "content": "{}", "edits": []}],
               "connections": [], "proposed_checks": [], "work_complete": True,
               "remaining_subtasks": [], "notes_for_qa": [], "user_actions": []}
        with self.assertRaises(ApplyError):
            self.wf._apply_development_implement(task, out)
        self.assertFalse((P.state / "settings.json").exists())

    def test_user_changes_preserved(self):
        pid = self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        ws = P.workspace(self.wf.project(pid)["slug"])
        gitops.ensure_repo(ws)
        (ws / "mine.txt").write_text("사용자가 수정 중", encoding="utf-8")
        cyc = self.db.one("SELECT * FROM cycles")
        self.db.update("cycles", cyc["id"], approved=1)
        task = {"id": 98, "project_id": pid, "cycle_id": cyc["id"], "dept": "development"}
        out = {"summary": "x", "changes": [{"path": "mine.txt", "op": "replace", "content": "덮어쓰기",
                                            "edits": []}], "connections": [], "proposed_checks": [],
               "work_complete": True, "remaining_subtasks": [], "notes_for_qa": [], "user_actions": []}
        with self.assertRaises(ApplyError):
            self.wf._apply_development_implement(task, out)
        self.assertEqual((ws / "mine.txt").read_text(encoding="utf-8"), "사용자가 수정 중")

    def test_check_commands_restricted(self):
        allow = ["python -m unittest", "npm test"]
        self.assertEqual(validate_check_command("npm  test", allow), "npm test")
        for bad in ["rm -rf /", "npm test && curl x", "git reset --hard", "powershell -c x",
                    "node evil.js", "python -m unittest; del x"]:
            with self.assertRaises(SafetyError, msg=bad):
                validate_check_command(bad, allow)

    def test_secrets_redacted(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-SECRETVALUE123456"
        try:
            text = redact("key=sk-ant-api03-SECRETVALUE123456 token: abcdefghijkl Bearer abcdefghijklmnopqrstu")
            self.assertNotIn("SECRETVALUE", text)
            self.assertNotIn("abcdefghijkl ", text)
            self.db.event("x", "API 키 sk-ant-api03-SECRETVALUE123456 노출 시도")
            self.assertNotIn("SECRETVALUE", self.db.one("SELECT message FROM events WHERE kind='x'")["message"])
        finally:
            del os.environ["ANTHROPIC_API_KEY"]

    def test_qa_fails_when_tests_weakened(self):
        pid = self.demo_to_release()
        proj = self.wf.project(pid)
        ws = P.workspace(proj["slug"])
        cyc_id = self.db.insert("cycles", project_id=pid, kind="bugfix", approved=1,
                                base_commit=gitops.head(ws), created_at="x")
        (ws / "tests" / "test_app.py").write_text("import unittest\n", encoding="utf-8")
        gitops.commit_paths(ws, ["tests/test_app.py"], "테스트 약화")
        tid = self.wf.create_task(pid, "qa.verify", "검증", {}, cycle_id=cyc_id, code_version="w")
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        results, findings = self.wf.qa_precheck(task)
        self.assertTrue(any("테스트 축소" in f["description"] for f in findings))

    def test_qa_fails_without_calling_model_when_check_fails(self):
        pid = self.demo_to_release()
        proj = self.wf.project(pid)
        ws = P.workspace(proj["slug"])
        (ws / "index.html").write_text("<p>깨진 화면</p>", encoding="utf-8")
        gitops.commit_paths(ws, ["index.html"], "깨뜨림")
        cyc_id = self.db.insert("cycles", project_id=pid, kind="bugfix", approved=1,
                                base_commit=gitops.head(ws), created_at="x")
        self.wf.create_task(pid, "qa.verify", "검증", {}, cycle_id=cyc_id, code_version="b")
        calls_before = self.db.one("SELECT COUNT(*) n FROM usage")["n"]
        self.run_ticks(10, until=lambda: self.db.one("SELECT 1 FROM qa_results WHERE cycle_id=?", (cyc_id,)))
        qa = self.db.one("SELECT * FROM qa_results WHERE cycle_id=?", (cyc_id,))
        self.assertEqual(qa["verdict"], "fail")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM usage")["n"], calls_before)  # 모델 호출 없음
        # 개발로 반려
        self.assertTrue(self.db.one("SELECT 1 FROM tasks WHERE cycle_id=? AND kind='development.implement'",
                                    (cyc_id,)))


class TrivialCheckTest(TeamCase):
    def test_echo_script_not_accepted_as_evidence(self):
        pid = self.wf.create_project("p", "아이디어 열 글자 이상입니다", demo=True)
        ws = P.workspace(self.wf.project(pid)["slug"])
        gitops.ensure_repo(ws)
        (ws / "package.json").write_text('{"scripts":{"lint":"echo \'Lint check passed\'","test":"node --test"}}',
                                         encoding="utf-8")
        f = self.wf._trivial_checks(ws, ["npm run lint", "npm test", "npm run build"])
        text = " ".join(x["description"] for x in f)
        self.assertIn("아무것도 검사하지 않는", text)
        self.assertIn("스크립트가 없습니다: npm run build", text)
        self.assertNotIn("test", " ".join(x["evidence"] for x in f if "lint" not in x["evidence"] and "build" not in x["description"]))


class CheckMergeTest(TeamCase):
    def test_setup_first_single_and_never_removed(self):
        from aiteam.workflow import merge_checks
        cur = merge_checks([], ["npm run build", "npm test", "npm ci --ignore-scripts"])
        self.assertEqual(cur[0], "npm ci --ignore-scripts")
        cur = merge_checks(cur, ["npm install --ignore-scripts", "rm -rf x", "npm run lint"])
        self.assertEqual(cur, ["npm install --ignore-scripts", "npm run build", "npm test", "npm run lint"])
        cur = merge_checks(cur, [])  # 제안이 없어도 기존 검증은 유지
        self.assertIn("npm test", cur)


class ExcerptTest(TeamCase):
    def test_error_message_kept_over_stack_trace(self):
        from aiteam.checks import excerpt
        out = "vite build\nerror during build:\nCould not resolve entry module \"index.html\".\n" + \
              "\n".join(f"    at fn{i} (node_modules/x.js:{i})" for i in range(200))
        e = excerpt(out)
        self.assertIn("Could not resolve entry module", e)
        self.assertNotIn("at fn150", e)
