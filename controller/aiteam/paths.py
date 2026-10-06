"""AI팀 폴더 경로. 기본은 이 저장소가 놓인 위치(D:\\AI-Team 권장)."""
import os
from pathlib import Path

DIR_NAMES = ["controller", "dashboard", "departments", "projects", "inbox",
             "artifacts", "state", "logs", "backups", "temp"]


class _Paths:
    def __init__(self):
        self.configure(os.environ.get("AI_TEAM_ROOT") or Path(__file__).resolve().parents[2])

    def configure(self, root):
        self.root = Path(root).resolve()
        for name in DIR_NAMES:
            setattr(self, name, self.root / name)
        self.db_file = self.state / "team.db"
        self.settings_file = self.state / "settings.json"
        self.token_file = self.state / "admin_token.txt"
        self.salt_file = self.state / "customer_salt.txt"
        self.journal = self.state / "journal"
        self.default_settings = self.controller / "settings.default.json"
        # 개발 중 테스트용으로 루트를 바꿀 때도 부서·화면 파일은 저장소의 것을 쓴다.
        repo = Path(__file__).resolve().parents[2]
        self.repo_departments = repo / "departments"
        self.repo_dashboard = repo / "dashboard"
        self.repo_default_settings = repo / "controller" / "settings.default.json"

    def ensure(self):
        for name in DIR_NAMES:
            getattr(self, name).mkdir(parents=True, exist_ok=True)
        self.journal.mkdir(parents=True, exist_ok=True)
        (self.logs / "calls").mkdir(parents=True, exist_ok=True)

    def project_dir(self, slug):
        return self.projects / slug

    def workspace(self, slug):
        return self.projects / slug / "workspace"

    def artifact_dir(self, slug):
        return self.artifacts / slug

    def inbox_dir(self, slug):
        return self.inbox / slug


P = _Paths()
