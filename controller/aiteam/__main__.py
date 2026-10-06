"""사용법: python -m aiteam [run|stop|open|doctor|status|test]"""
import json
import os
import sys
import urllib.request
import webbrowser

from . import settings
from .paths import P


def _admin_url():
    return f"http://127.0.0.1:{settings.load()['ports']['admin']}"


def cmd_run():
    from . import logs
    from .db import DB
    from .scheduler import Scheduler
    from .server import _on_shutdown, admin_token, serve
    P.ensure()
    log = logs.setup()
    db = DB()
    sched = Scheduler(db)
    try:
        admin, public = serve(db, sched, sched.wf, block=False)
    except OSError:
        print("포트가 사용 중입니다. 이미 실행 중이면 open-dashboard.bat 으로 화면만 여세요.")
        sys.exit(1)
    sched.start_thread()
    _on_shutdown.append(lambda: sched.shutdown(60))
    s = settings.load()
    admin_token()
    log.info("AI팀 실행 제어 프로그램 시작 (상태: %s)", sched.team_state())
    print(f"\n관리 화면: {_admin_url()}  (open-dashboard.bat 으로 열기)")
    print(f"고객 문의 화면: http://{s['ports']['public_bind']}:{s['ports']['public']}/support?p=<프로젝트>")
    print("이 창을 닫으면 AI팀이 멈춥니다. 중지는 stop-team.bat 을 쓰세요.\n")
    try:
        admin.serve_forever()
    except KeyboardInterrupt:
        sched.shutdown(30)
    log.info("AI팀 종료")


def _post(path, data):
    from .server import admin_token
    req = urllib.request.Request(_admin_url() + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json", "X-AITeam": "1",
                                          "Cookie": f"aiteam_admin={admin_token()}",
                                          "Host": "127.0.0.1"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def cmd_stop():
    try:
        _post("/api/shutdown", {})
        print("종료 요청을 보냈습니다. 진행 중 작업은 안전한 지점까지 마무리 후 멈춥니다(최대 1분).")
    except Exception:
        print("실행 중인 AI팀을 찾지 못했습니다.")


def cmd_open():
    from .server import admin_token
    P.ensure()
    url = f"{_admin_url()}/#t={admin_token()}"
    webbrowser.open(url)
    print(f"브라우저에서 관리 화면을 엽니다: {_admin_url()}")


def cmd_status():
    from .server import admin_token
    req = urllib.request.Request(_admin_url() + "/api/state",
                                 headers={"Cookie": f"aiteam_admin={admin_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            st = json.loads(r.read())
    except Exception:
        print("실행 중이 아닙니다.")
        return
    print(f"팀 상태: {st['team']['state_ko']}")
    for d in st["departments"]:
        print(f"- {d['name']}: {d['status_ko']} {d['current'] or ''}")


def cmd_test():
    import unittest
    tests = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests")
    suite = unittest.defaultTestLoader.discover(tests)
    r = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if r.wasSuccessful() else 1)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "doctor":
        from .doctor import report
        return report()
    {"run": cmd_run, "stop": cmd_stop, "open": cmd_open, "status": cmd_status,
     "test": cmd_test}.get(cmd, lambda: print(__doc__))()


if __name__ == "__main__":
    main()
