"""사용자가 허용한 검증 명령만 실행한다(셸 사용 안 함)."""
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
        results.append({"command": c, "ok": code == 0, "exit": code,
                        "output": out[-4000:], "seconds": round(time.time() - t0, 1)})
    return results


def kill_all():
    with _lock:
        procs = list(_running.values())
    for p in procs:
        kill_tree(p)
