"""사용자가 허용한 검증 명령만 실행한다(셸 사용 안 함)."""
import re
import subprocess
import threading
import time

from . import settings
from .safety import (SafetyError, kill_tree, popen_kwargs, safe_env, split_command,
                     validate_check_command)

_running = {}
_lock = threading.Lock()


def run_checks(ws, commands, task_id, timeout=600):
    allow = settings.load()["check_command_allowlist"]
    results = []
    for cmd in commands:
        try:
            c = validate_check_command(cmd, allow)
        except SafetyError as e:
            results.append({"command": cmd, "ok": False, "exit": None, "blocked": True,
                            "output": str(e), "seconds": 0})
            continue
        t0 = time.time()
        try:
            proc = subprocess.Popen(split_command(c), cwd=str(ws), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", env=safe_env(), **popen_kwargs())
        except OSError as e:
            results.append({"command": c, "ok": False, "exit": None, "output": f"실행 불가: {e}",
                            "seconds": 0})
            continue
        with _lock:
            _running[task_id] = proc
        try:
            out, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            out, code = "시간 초과", None
        finally:
            with _lock:
                _running.pop(task_id, None)
        out = out or ""
        if len(out) > 6000:
            out = out[:2500] + "\n... (중간 생략) ...\n" + out[-3000:]
        results.append({"command": c, "ok": code == 0, "exit": code,
                        "output": out, "seconds": round(time.time() - t0, 1)})
    return results


_ERR = re.compile(r"error|fail|not ok|cannot|could not|unable|missing|expected|assert|✖|✗|ERR_|"
                  r"오류|실패", re.I)
_NOISE = re.compile(r"^\s+at |node:internal|^\s*(stack|duration_ms|type|location):", re.I)


def excerpt(output, limit=1800):
    """검증 실패 출력에서 원인 문장을 추린다(스택 추적보다 오류 메시지 우선)."""
    lines = [l.rstrip() for l in (output or "").splitlines() if l.strip()]
    picked, seen = [], set()
    for l in lines:
        if _ERR.search(l) and not _NOISE.search(l) and l not in seen:
            seen.add(l)
            picked.append(l[:300])
    text = "\n".join(picked)
    if len(text) > limit:
        text = text[:limit] + "\n..."
    tail = "\n".join(l for l in lines[-8:] if not _NOISE.search(l))
    return (text + "\n--- 끝부분 ---\n" + tail)[: limit + 600] if text else tail[-limit:]


def kill_all():
    with _lock:
        procs = list(_running.values())
    for p in procs:
        kill_tree(p)
