import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiteam.paths import P  # noqa: E402


class TeamCase(unittest.TestCase):
    """테스트마다 빈 AI팀 폴더를 만든다(모의 호출 사용)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiteam-test-")
        P.configure(self.tmp)
        P.ensure()
        from aiteam.db import DB
        from aiteam.runner import MockRunner
        from aiteam.scheduler import Scheduler
        self.db = DB()
        self.mock = MockRunner(delay=0.02)
        self.s = Scheduler(self.db, mock_runner=self.mock, tick_sec=0.05)
        self.wf = self.s.wf

    def tearDown(self):
        self.s.shutdown(5)
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settings(self, patch):
        from aiteam import settings
        return settings.update(patch)

    def run_ticks(self, n=30, until=None):
        for _ in range(n):
            self.s.last_house = 0
            self.s.tick()
            time.sleep(0.05)
            with self.s.lock:
                busy = bool(self.s.running)
            if until and until() and not busy:
                return True
        # 실행 중인 작업 마무리
        for _ in range(100):
            with self.s.lock:
                if not self.s.running:
                    break
            time.sleep(0.05)
        return until() if until else None

    def task(self, kind):
        return self.db.one("SELECT * FROM tasks WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))

    def pending(self, kind):
        return self.db.one("SELECT * FROM decisions WHERE kind=? AND status='pending'", (kind,))

    def demo_to_release(self):
        """모의 데모 프로젝트를 첫 배포까지 진행."""
        pid = self.wf.create_project("todo", "바쁜 직장인을 위한 할 일 앱입니다", demo=True)
        self.s.control("start")
        self.run_ticks(until=lambda: self.pending("mvp_direction"))
        self.wf.answer_decision(self.pending("mvp_direction")["id"], {"direction_id": "A"})
        self.run_ticks(80, until=lambda: self.pending("deploy_approval"))
        return pid
