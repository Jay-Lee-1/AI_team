"""예산에 따른 호출 중지, 부서별 실제 사용량 기록, 실제 CLI 응답 해석."""
import json

from helpers import TeamCase

from aiteam import budget
from aiteam.runner import ClaudeCliRunner, RunResult

# 실제 Claude Code CLI(2.1.x) 응답에서 필요한 부분만 옮긴 예시
REAL_CLI_JSON = json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "session_id": "s-1",
    "uuid": "u-1", "total_cost_usd": 0.012129,
    "usage": {"input_tokens": 10, "output_tokens": 241},
    "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 915, "outputTokens": 252,
                                                 "cacheReadInputTokens": 0,
                                                 "cacheCreationInputTokens": 4977,
                                                 "costUSD": 0.012129}},
    "result": "{\"ok\":true}", "structured_output": {"ok": True}})


class BudgetTest(TeamCase):
    def _real_project(self):
        return self.wf.create_project("real", "실제 호출 프로젝트입니다", demo=False)

    def test_no_paid_calls_before_auth_and_budget(self):
        self._real_project()
        self.s.control("start")
        self.run_ticks(3)
        self.assertEqual(self.task("planning.define_mvp")["status"], "queued")
        self.assertIn("예산", str(self.db.meta_get("blocked_reasons")))

    def test_budget_limit_stops_new_calls(self):
        self.settings({"auth": {"mode": "subscription"},
                       "budget": {"daily_usd": 1.0, "monthly_usd": 20, "per_call_usd": 0.3,
                                  "safety_margin_ratio": 0.1}})
        self.assertEqual(budget.gate(self.db, 1, False)[0], "ok")
        from aiteam.db import local_day
        d = local_day()
        self.db.x("INSERT INTO usage(task_id,attempt,dept,model,cost_usd,status,day,month) "
                  "VALUES(1,1,'planning','m',0.65,'collected',?,?)", (d, d[:7]))
        level, reason = budget.gate(self.db, 1, False)
        self.assertEqual(level, "stop")  # 0.65 + 0.3 > 1.0 * 0.9
        self.assertIn("하루 예산", reason)
        self._real_project()
        self.s.control("start")
        self.run_ticks(3)
        self.assertEqual(self.task("planning.define_mvp")["status"], "queued")

    def test_slowdown_near_limit_keeps_only_core_work(self):
        self.settings({"auth": {"mode": "api_key"},
                       "budget": {"daily_usd": 10, "monthly_usd": 100, "per_call_usd": 0.5,
                                  "slowdown_ratio": 0.8, "safety_margin_ratio": 0.05}})
        from aiteam.db import local_day
        d = local_day()
        self.db.x("INSERT INTO usage(task_id,attempt,dept,model,cost_usd,status,day,month) "
                  "VALUES(1,1,'qa','m',8.2,'collected',?,?)", (d, d[:7]))
        self.assertEqual(budget.gate(self.db, 1, False)[0], "slow")
        self.assertEqual(budget.gate(self.db, 5, False)[0], "stop")

    def test_reserved_running_calls_count_toward_budget(self):
        self.settings({"auth": {"mode": "subscription"},
                       "budget": {"daily_usd": 1.0, "monthly_usd": 20, "per_call_usd": 0.4}})
        task = {"id": 5, "project_id": 1, "dept": "design"}
        budget.reserve(self.db, task, 1, "sonnet", 0.4, False)
        self.assertEqual(budget.gate(self.db, 1, False)[0], "ok")
        budget.reserve(self.db, {"id": 6, "project_id": 1, "dept": "qa"}, 1, "sonnet", 0.4, False)
        self.assertEqual(budget.gate(self.db, 1, False)[0], "stop")
        t = budget.totals(self.db)
        self.assertEqual(t["day"]["collecting"], 2)

    def test_real_cli_usage_parsed_and_recorded_per_department(self):
        res = ClaudeCliRunner()._parse(REAL_CLI_JSON, "", False)
        self.assertTrue(res.ok)
        self.assertEqual(res.output, {"ok": True})
        u = res.usage["claude-haiku-4-5-20251001"]
        self.assertEqual((u["input"], u["output"], u["cache_write"]), (915, 252, 4977))
        task = {"id": 7, "project_id": 1, "dept": "design"}
        budget.reserve(self.db, task, 1, "haiku", 0.5, False)
        budget.record(self.db, task, 1, res)
        budget.record(self.db, task, 1, res)  # 같은 결과 두 번 → 중복 합산 안 함
        rows = self.db.all("SELECT * FROM usage")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dept"], "design")
        by = budget.by_dept(self.db)
        self.assertEqual(by["design"]["inp"], 915)
        self.assertAlmostEqual(budget.totals(self.db)["day"]["cost"], 0.012129)

    def test_missing_usage_is_not_zero(self):
        task = {"id": 8, "project_id": 1, "dept": "qa"}
        budget.record(self.db, task, 1, RunResult(False, usage={}, cost_usd=None))
        row = self.db.one("SELECT * FROM usage")
        self.assertEqual(row["status"], "missing")
        self.assertIsNone(row["input_tokens"])

    def test_error_classification(self):
        bad = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "api_error_status": 429, "result": "rate limit", "modelUsage": {}})
        self.assertEqual(ClaudeCliRunner()._parse(bad, "", False).error_kind, "rate_limit")
        bud = json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                          "modelUsage": {}})
        self.assertEqual(ClaudeCliRunner()._parse(bud, "", False).error_kind, "budget")
        self.assertEqual(ClaudeCliRunner()._parse("", "Invalid API key", False).error_kind, "auth")
