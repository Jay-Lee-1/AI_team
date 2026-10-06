"""환경 확인. 설치하거나 바꾸지 않고 확인만 한다."""
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import VERSION, settings
from .paths import P
from .safety import resolve_executable


def _run(args, timeout=30):
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return (r.stdout or r.stderr).strip()
    except Exception as e:
        return f"(실행 실패: {type(e).__name__})"


def _dir_size_gb(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / 1024 ** 3, 2)


def _port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def check():
    s = settings.load()
    items = []

    def add(name, ok, detail, todo=None):
        items.append({"항목": name, "상태": {True: "확인", False: "문제", None: "미확인"}[ok],
                      "내용": detail, "할 일": todo})

    add("운영체제", True, f"{platform.system()} {platform.release()}")
    add("AI팀 프로그램", True, f"버전 {VERSION}")
    root = str(P.root)
    drive = os.path.splitdrive(root)[0]
    if os.name == "nt":
        add("설치 위치", drive.upper() == s["required_drive"].upper(), root,
            None if drive.upper() == s["required_drive"].upper() else "D:\\AI-Team 에 두는 것을 권장합니다")
        add("D 드라이브", os.path.exists("D:\\"), "있음" if os.path.exists("D:\\") else "없음",
            None if os.path.exists("D:\\") else "D 드라이브가 없습니다. 다른 위치를 쓸지 알려주세요")
    else:
        add("설치 위치", None, f"{root} (Windows가 아니어서 D 드라이브 확인 생략)")
    try:
        u = shutil.disk_usage(root)
        free = round(u.free / 1024 ** 3, 1)
        add("여유 공간", free >= s["min_free_gb"], f"{free} GB",
            None if free >= s["min_free_gb"] else f"{s['min_free_gb']}GB 이상 확보 필요")
    except OSError:
        add("여유 공간", None, "확인 불가")
    add("Python", sys.version_info >= (3, 10), sys.version.split()[0],
        None if sys.version_info >= (3, 10) else "Python 3.10 이상 설치 필요")
    git = shutil.which("git")
    add("Git", bool(git), _run(["git", "--version"]) if git else "없음",
        None if git else "Git for Windows 설치 필요(git-scm.com) — 복구 지점에 필요")
    exe = resolve_executable("claude")
    if exe:
        ver = _run([*exe, "--version"])
        add("Claude Code", True, ver)
        st = _run([*exe, "auth", "status"])
        try:
            j = json.loads(st)
            add("Claude 인증", bool(j.get("loggedIn")),
                f"로그인={j.get('loggedIn')}, 방식={j.get('authMethod')}",
                None if j.get("loggedIn") else "cmd에서 claude 실행 후 /login")
        except ValueError:
            add("Claude 인증", None, "상태를 읽지 못함", "cmd에서 claude 실행 후 /login 으로 확인")
        cfg = Path.home() / ".claude"
        if cfg.exists():
            add("C 드라이브 사용(Claude 설정)", True,
                f"{cfg} 약 {_dir_size_gb(cfg)} GB — 인증·설정 저장소라 옮기거나 지우지 않습니다. "
                "AI팀 호출은 세션을 저장하지 않게 실행합니다")
    else:
        add("Claude Code", False, "없음",
            "PowerShell에서: irm https://claude.ai/install.ps1 | iex  (공식 안내: code.claude.com/docs)")
    add("API 키 환경 변수", None if not os.environ.get("ANTHROPIC_API_KEY") else True,
        "설정됨(값은 표시하지 않음)" if os.environ.get("ANTHROPIC_API_KEY") else "없음(구독 로그인 방식이면 필요 없음)")
    add("Node.js(앱 개발용, 선택)", None if not shutil.which("node") else True,
        _run(["node", "--version"]) if shutil.which("node") else "없음 — 웹 앱 도구가 필요해지면 안내합니다")
    for name, port in s["ports"].items():
        if isinstance(port, int):
            add(f"포트 {port}({name})", _port_free(port), "사용 가능" if _port_free(port) else "사용 중(이미 실행 중일 수 있음)")
    add("인증·예산 설정", settings.is_paid_ready(s),
        f"인증={s['auth'].get('mode')}, 하루={s['budget'].get('daily_usd')}, 월={s['budget'].get('monthly_usd')}",
        None if settings.is_paid_ready(s) else "관리 화면 > 설정에서 정하기 전에는 실제 호출을 하지 않습니다")
    return items


def report():
    print("\n=== AI팀 환경 확인 (설치·변경 없이 확인만) ===\n")
    for it in check():
        mark = {"확인": "[O]", "문제": "[X]", "미확인": "[?]"}[it["상태"]]
        print(f"{mark} {it['항목']}: {it['내용']}")
        if it["할 일"]:
            print(f"     → 할 일: {it['할 일']}")
    print()
