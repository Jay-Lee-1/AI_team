"""실행 코드로 적용하는 안전장치.
- 허용된 실제 경로 밖 쓰기 차단 (링크·junction 탈출 포함)
- 영구 삭제·강제 초기화·임의 셸 실행 차단
- 로그·화면의 비밀정보 가림
"""
import os
import re
import shlex
import signal
import subprocess
import sys
from pathlib import Path, PurePosixPath

IS_WIN = os.name == "nt"


class SafetyError(Exception):
    pass


# ───────── 비밀정보 가림 ─────────
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)((?:api[_-]?key|secret|password|passwd|token)\s*[=:]\s*)[^\s,;\"']{6,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]
_SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


def redact(text):
    if not text:
        return text
    text = str(text)
    for name in _SECRET_ENV:
        val = os.environ.get(name)
        if val and len(val) >= 8:
            text = text.replace(val, "[가림]")
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + "[가림]", text)
        else:
            text = pat.sub("[가림]", text)
    return text


# ───────── 경로 검사 ─────────
_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}
_PROTECTED_PARTS = {".git", ".claude", ".env"}


def _is_link_or_junction(p: Path):
    try:
        if p.is_symlink():
            return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(p):
            return True
        if IS_WIN and p.exists():
            # FILE_ATTRIBUTE_REPARSE_POINT
            return bool(os.lstat(p).st_file_attributes & 0x400)
    except OSError:
        return True
    return False


def check_relative(rel: str):
    """모델이 제시한 상대 경로의 형식 검사."""
    if not isinstance(rel, str) or not rel.strip():
        raise SafetyError("빈 경로")
    if "\x00" in rel:
        raise SafetyError("잘못된 문자")
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or norm.startswith("//"):
        raise SafetyError(f"절대 경로는 허용하지 않습니다: {rel}")
    parts = PurePosixPath(norm).parts
    for part in parts:
        if part in ("..", ""):
            raise SafetyError(f"상위 폴더 이동은 허용하지 않습니다: {rel}")
        if ":" in part:
            raise SafetyError(f"':' 문자는 허용하지 않습니다: {rel}")
        if part.split(".")[0].lower() in _WIN_RESERVED:
            raise SafetyError(f"Windows 예약 이름입니다: {rel}")
        if part.lower() in _PROTECTED_PARTS:
            raise SafetyError(f"보호된 경로입니다: {rel}")
        if part.endswith((" ", ".")) and part not in (".",):
            raise SafetyError(f"공백·마침표로 끝나는 이름은 허용하지 않습니다: {rel}")
    if len(parts) > 20 or len(norm) > 240:
        raise SafetyError("경로가 너무 깁니다")
    return norm


def safe_join(base: Path, rel: str) -> Path:
    """base 아래의 실제 경로만 돌려준다. 링크·junction으로 빠져나가면 거부."""
    norm = check_relative(rel)
    base_real = Path(os.path.realpath(base))
    target = base_real.joinpath(*PurePosixPath(norm).parts)
    # 존재하는 각 단계가 링크/junction인지 확인
    cur = base_real
    for part in PurePosixPath(norm).parts:
        cur = cur / part
        if cur.exists() or cur.is_symlink():
            if _is_link_or_junction(cur):
                raise SafetyError(f"링크·junction 경로는 허용하지 않습니다: {rel}")
    real = Path(os.path.realpath(target))
    try:
        real.relative_to(base_real)
    except ValueError:
        raise SafetyError(f"허용된 폴더 밖입니다: {rel}")
    return real


def assert_inside(path: Path, allowed_roots):
    real = Path(os.path.realpath(path))
    for root in allowed_roots:
        try:
            real.relative_to(Path(os.path.realpath(root)))
            return real
        except ValueError:
            continue
    raise SafetyError(f"허용된 폴더 밖 쓰기 차단: {path}")


def guarded_write(base: Path, rel: str, content: str, allowed_roots):
    target = safe_join(base, rel)
    assert_inside(target, allowed_roots)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 부모 폴더 생성 후 다시 확인 (경쟁 상황 대비)
    assert_inside(target.parent, allowed_roots)
    if target.exists() and _is_link_or_junction(target):
        raise SafetyError("링크 파일에는 쓰지 않습니다")
    tmp = target.with_name(target.name + ".aiteam-tmp")
    tmp.write_text(content, encoding="utf-8", newline="")
    os.replace(tmp, target)
    return target


# ───────── 명령 실행 제한 ─────────
_DANGEROUS = [
    r"\brm\b", r"\brmdir\b", r"\bdel\b", r"\berase\b", r"\bformat\b", r"\brd\b",
    r"reset\s+--hard", r"push\s+(-f|--force)", r"clean\s+-[a-z]*f", r"\bmkfs\b",
    r"Remove-Item", r"[|;&><`$]", r"\bcurl\b", r"\bwget\b", r"Invoke-WebRequest",
    r"\bshutdown\b", r"\breg\b", r"\bsetx\b", r"\bpowershell\b", r"\bcmd\b",
]


def validate_check_command(cmd: str, allowlist):
    """검증 명령은 허용 목록의 명령으로 시작해야 하며 셸 기능을 쓰지 않는다."""
    if not isinstance(cmd, str) or not cmd.strip():
        raise SafetyError("빈 명령")
    c = " ".join(cmd.split())
    for pat in _DANGEROUS:
        if re.search(pat, c, flags=re.I):
            raise SafetyError(f"허용하지 않는 명령입니다: {cmd}")
    if not any(c == a or c.startswith(a + " ") for a in allowlist):
        raise SafetyError(f"허용 목록에 없는 검증 명령입니다: {cmd}")
    return c


def split_command(cmd: str):
    parts = shlex.split(cmd, posix=True)
    if parts and parts[0] in ("python", "python3", "py"):
        parts[0] = sys.executable
    return parts


def popen_kwargs():
    if IS_WIN:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000}  # NO_WINDOW
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen):
    if proc.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def resolve_executable(name):
    """Windows의 .cmd 실행은 cmd.exe 인자 해석 문제가 있으므로 가능한 한 실제 실행파일을 찾는다."""
    import shutil
    found = shutil.which(name)
    if not found:
        return None
    if IS_WIN and found.lower().endswith((".cmd", ".bat")):
        base = Path(found).parent
        cli = base / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        node = shutil.which("node")
        if cli.exists() and node:
            return [node, str(cli)]
        return None
    return [found]


def safe_env(extra=None):
    """하위 프로세스 환경. 임시 파일과 캐시를 AI-Team/temp에 둔다."""
    from .paths import P
    env = dict(os.environ)
    tmp = str(P.temp)
    env.update({"TMP": tmp, "TEMP": tmp, "TMPDIR": tmp,
                "npm_config_cache": str(P.temp / "npm-cache"),
                "PIP_CACHE_DIR": str(P.temp / "pip-cache"),
                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                "CI": "1", "NO_COLOR": "1"})
    if extra:
        env.update(extra)
    return env
