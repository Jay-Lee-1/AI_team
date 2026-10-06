"""작업 사본의 Git 상태 확인과 복구 지점. 강제 초기화·강제 푸시·히스토리 재작성은 하지 않는다."""
import shutil
import subprocess
from pathlib import Path

from .safety import safe_env


class GitError(Exception):
    pass


def available():
    return shutil.which("git") is not None


def _git(ws: Path, *args, check=True, timeout=60):
    r = subprocess.run(["git", *args], cwd=str(ws), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=safe_env())
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} 실패: {r.stderr.strip()[:300]}")
    return r.stdout


def ensure_repo(ws: Path):
    ws.mkdir(parents=True, exist_ok=True)
    if not (ws / ".git").exists():
        _git(ws, "init", "-q")
        _git(ws, "config", "user.name", "AI-Team")
        _git(ws, "config", "user.email", "ai-team@localhost")
        _git(ws, "config", "core.autocrlf", "false")
        (ws / ".gitignore").write_text("node_modules/\n__pycache__/\n.env\n", encoding="utf-8") \
            if not (ws / ".gitignore").exists() else None
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "--allow-empty", "-m", "AI-Team: 작업 사본 시작")


def head(ws: Path):
    try:
        return _git(ws, "rev-parse", "HEAD").strip()
    except GitError:
        return ""


def dirty_files(ws: Path):
    out = _git(ws, "status", "--porcelain", "-uall")
    files = []
    for line in out.splitlines():
        if len(line) > 3:
            path = line[3:].strip().strip('"')
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append(path.replace("\\", "/"))
    return files


def commit_paths(ws: Path, paths, message):
    if not paths:
        return head(ws)
    _git(ws, "add", "--", *paths)
    staged = _git(ws, "diff", "--cached", "--name-only").strip()
    if staged:
        _git(ws, "commit", "-q", "-m", message)
    return head(ws)


def diff_since(ws: Path, base: str, max_chars=60000):
    if not base:
        return ""
    out = _git(ws, "diff", "--stat", base, "HEAD") + "\n" + _git(ws, "diff", base, "HEAD")
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n... (이하 {len(out) - max_chars}자 생략)"
    return out


def files_changed_since(ws: Path, base: str):
    if not base:
        return []
    return [l for l in _git(ws, "diff", "--name-only", base, "HEAD").splitlines() if l]


def file_at(ws: Path, rev: str, path: str):
    r = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=str(ws), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=safe_env())
    return r.stdout if r.returncode == 0 else None


def ls_files(ws: Path, limit=400):
    files = _git(ws, "ls-files").splitlines()
    return files[:limit], len(files)


def import_source(src: Path, ws: Path):
    """기존 앱을 작업 사본으로 가져온다. 원본은 읽기만 한다."""
    if ws.exists() and any(ws.iterdir()):
        return "already"
    ws.parent.mkdir(parents=True, exist_ok=True)
    if (src / ".git").exists():
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(src), str(ws)], check=True,
                       capture_output=True, env=safe_env(), timeout=600)
        _git(ws, "checkout", "-q", "-b", "ai-team/work")
        _git(ws, "config", "user.name", "AI-Team")
        _git(ws, "config", "user.email", "ai-team@localhost")
        return "cloned"
    ignore = shutil.ignore_patterns("node_modules", ".venv", "venv", "__pycache__", "build",
                                    "dist", ".gradle", ".dart_tool", "*.exe", "*.dll")
    shutil.copytree(src, ws, symlinks=True, ignore=ignore)
    ensure_repo(ws)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "AI-Team: 기존 앱 사본 가져오기")
    return "copied"
