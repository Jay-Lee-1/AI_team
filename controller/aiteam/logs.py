"""파일 로그. 비밀정보를 가리고 보관 기간이 지난 로그는 정리한다."""
import logging
import time
from datetime import datetime

from .paths import P
from .safety import redact


class _Redact(logging.Filter):
    def filter(self, record):
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(redact(str(a)) for a in record.args)
        return True


def setup():
    P.logs.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("aiteam")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(P.logs / f"controller-{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh.addFilter(_Redact())
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    sh.addFilter(_Redact())
    log.addHandler(sh)
    return log


def save_call(task_id, attempt, raw):
    path = P.logs / "calls" / f"task-{task_id}-try{attempt}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(raw or "")[:500000], encoding="utf-8")


def cleanup(days):
    """보관 기간이 지난 AI팀 자체 로그만 정리한다(프로젝트 파일은 건드리지 않는다)."""
    cutoff = time.time() - days * 86400
    n = 0
    for f in list(P.logs.glob("*.log")) + list((P.logs / "calls").glob("*.json")):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n
