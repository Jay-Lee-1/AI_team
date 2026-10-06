"""두 개의 웹 서버.
- 관리 서버: 127.0.0.1 전용. 관리 화면·운영 제어. 관리자 토큰(쿠키) 필요.
- 고객 서버: 문의 접수 화면과 API만. 다른 고객 정보나 운영 제어에 접근할 수 없다."""
import hmac
import json
import mimetypes
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import inquiries, settings, views
from .db import jl
from .paths import P
from .safety import SafetyError, safe_join

MAX_BODY = 6 * 1024 * 1024


def admin_token():
    if not P.token_file.exists():
        P.token_file.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    return P.token_file.read_text(encoding="utf-8").strip()


class _Base(BaseHTTPRequestHandler):
    server_version = "AITeam"
    sys_version = ""

    def log_message(self, fmt, *args):  # 접속 로그에 토큰·개인정보를 남기지 않는다
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("요청이 너무 큽니다")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _static(self, folder, name, extra=None):
        try:
            path = safe_join(folder, name)
        except SafetyError:
            return self._send(404, {"error": "없음"})
        if not path.is_file():
            return self._send(404, {"error": "없음"})
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype, extra)


# ───────────────── 관리 서버 ─────────────────
class AdminHandler(_Base):
    app = None  # (db, sched, wf)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _authed(self):
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"aiteam_admin=([A-Za-z0-9_\-]+)", cookie)
        return bool(m) and hmac.compare_digest(m.group(1), admin_token())

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "허용되지 않은 주소"})
        u = urlparse(self.path)
        path = unquote(u.path)
        dash = P.dashboard if (P.dashboard / "index.html").exists() else P.repo_dashboard
        if path in ("/", "/index.html"):
            return self._static(dash, "index.html")
        if path in ("/app.js", "/style.css"):
            return self._static(dash, path[1:])
        if not self._authed():
            return self._send(401, {"error": "관리자 로그인이 필요합니다. open-dashboard.bat으로 여세요"})
        db, sched, wf = self.app
        if path == "/api/state":
            q = parse_qs(u.query)
            pid = int(q["project"][0]) if q.get("project") and q["project"][0].isdigit() else None
            return self._send(200, views.state(db, sched, pid))
        if path.startswith("/artifact/"):
            return self._static(P.artifacts, path[len("/artifact/"):],
                                {"Content-Security-Policy": "sandbox; default-src 'none'; "
                                                            "style-src 'unsafe-inline'; img-src data:"})
        m = re.fullmatch(r"/api/inquiries/(\d+)/attachment/(\d+)", path)
        if m:
            row = db.one("SELECT i.attachments_json, p.slug FROM inquiries i JOIN projects p ON "
                         "p.id=i.project_id WHERE i.id=?", (int(m.group(1)),))
            atts = jl(row["attachments_json"], []) if row else []
            k = int(m.group(2))
            if not row or k >= len(atts):
                return self._send(404, {"error": "없음"})
            return self._static(P.inbox_dir(row["slug"]) / "attachments", atts[k]["file"],
                                {"Content-Disposition": "attachment",
                                 "Content-Security-Policy": "sandbox"})
        self._send(404, {"error": "없음"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "허용되지 않은 주소"})
        path = urlparse(self.path).path
        try:
            body = self._json_body()
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        if path == "/api/login":
            if hmac.compare_digest(str(body.get("token") or ""), admin_token()):
                return self._send(200, {"ok": True}, headers={
                    "Set-Cookie": f"aiteam_admin={admin_token()}; HttpOnly; SameSite=Strict; Path=/"})
            return self._send(403, {"error": "토큰이 맞지 않습니다"})
        if not self._authed() or self.headers.get("X-AITeam") != "1":
            return self._send(401, {"error": "관리자 인증 필요"})
        db, sched, wf = self.app
        try:
            result = self._route(path, body, db, sched, wf)
            return self._send(200, {"ok": True, "result": result})
        except (ValueError, KeyError, SafetyError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # 화면에는 짧게만
            return self._send(500, {"error": f"{type(e).__name__}: {e}"[:300]})

    def _route(self, path, b, db, sched, wf):
        if path == "/api/control":
            return sched.control(b["action"], b.get("scope", "global"), b.get("target"))
        if path == "/api/settings":
            return settings.update(b)
        if path == "/api/projects":
            name = (b.get("name") or "").strip()
            idea = (b.get("idea") or "").strip()
            if not name or len(idea) < 10:
                raise ValueError("앱 이름과 아이디어(10자 이상)를 입력하세요")
            src = (b.get("source_path") or "").strip()
            pid = wf.create_project(name[:60], idea[:8000], (b.get("customers") or "")[:2000],
                                    src, bool(b.get("demo")))
            if src:
                from pathlib import Path
                from . import gitops
                sp = Path(src)
                if not sp.is_dir():
                    raise ValueError("기존 코드 폴더를 찾지 못했습니다")
                how = gitops.import_source(sp, P.workspace(wf.project(pid)["slug"]))
                db.event("project", f"기존 앱 작업 사본 준비({how}). 원본은 수정하지 않습니다", project_id=pid)
            return {"project_id": pid}
        m = re.fullmatch(r"/api/projects/(\d+)/settings", path)
        if m:
            proj = wf.project(int(m.group(1)))
            allow = settings.load()["check_command_allowlist"]
            patch = {}
            if "checks" in b:
                from .safety import validate_check_command
                from .workflow import order_checks
                patch["checks"] = order_checks([validate_check_command(c, allow)
                                                for c in b["checks"] if c.strip()][:10])
            if "deploy_command" in b:
                cmd = (b["deploy_command"] or "").strip()
                if cmd:
                    from .safety import validate_check_command
                    validate_check_command(cmd, [cmd.split()[0]])
                patch["deploy_command"] = cmd
            if "preview_path" in b:
                patch["preview_path"] = b["preview_path"].strip() or "index.html"
            wf.set_psettings(proj, patch)
            db.event("settings", "프로젝트 설정 변경(사용자)", project_id=proj["id"])
            return patch
        m = re.fullmatch(r"/api/decisions/(\d+)/answer", path)
        if m:
            return wf.answer_decision(int(m.group(1)), b)
        m = re.fullmatch(r"/api/proposals/(\d+)/decide", path)
        if m:
            return wf.decide_proposal(int(m.group(1)), b.get("choice"), b.get("scope") or "")
        m = re.fullmatch(r"/api/clusters/(\d+)/confirm", path)
        if m:
            return wf.confirm_resolved(int(m.group(1)))
        m = re.fullmatch(r"/api/tasks/(\d+)/retry", path)
        if m:
            return sched.retry_task(int(m.group(1)))
        if path == "/api/shutdown":
            threading.Thread(target=_shutdown_all, daemon=True).start()
            return "종료 중"
        raise ValueError("알 수 없는 요청")


# ───────────────── 앱 미리보기 서버 ─────────────────
class PreviewHandler(_Base):
    """작업 사본의 앱을 다른 포트(다른 출처)에서 보여준다.
    AI가 만든 코드가 관리 화면의 쿠키·API에 접근하지 못하게 분리한다."""

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return self._send(403, {"error": "허용되지 않은 주소"})
        m = re.fullmatch(r"/([a-z0-9-]+)/(.*)", unquote(urlparse(self.path).path))
        if not m:
            return self._send(404, {"error": "없음"})
        return self._static(P.workspace(m.group(1)), m.group(2) or "index.html",
                            {"Cache-Control": "no-store"})


# ───────────────── 고객 서버 ─────────────────
class PublicHandler(_Base):
    app = None
    hits = {}
    hits_lock = threading.Lock()

    def _cors(self):
        allowed = settings.load()["public_allowed_origins"]
        origin = self.headers.get("Origin")
        if "*" in allowed:
            return {"Access-Control-Allow-Origin": "*"}
        if origin in allowed:
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        return {}

    def _rate_ok(self, limit=8, window=60):
        ip = self.client_address[0]
        t = time.time()
        with self.hits_lock:
            q = [x for x in self.hits.get(ip, []) if t - x < window]
            if len(q) >= limit:
                self.hits[ip] = q
                return False
            q.append(t)
            self.hits[ip] = q
        return True

    def do_OPTIONS(self):
        self._send(204, b"", headers={**self._cors(), "Access-Control-Allow-Methods": "POST, GET",
                                      "Access-Control-Allow-Headers": "Content-Type",
                                      "Access-Control-Max-Age": "600"})

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        dash = P.dashboard if (P.dashboard / "support.html").exists() else P.repo_dashboard
        if path in ("/support", "/support.html"):
            return self._static(dash, "support.html")
        if path in ("/widget.js", "/support.js", "/support.css"):
            return self._static(dash, path[1:], self._cors())
        if path == "/api/public/status":
            q = parse_qs(u.query)
            if not self._rate_ok(30):
                return self._send(429, {"error": "잠시 후 다시 시도하세요"})
            r = inquiries.lookup(self.app[0], (q.get("receipt") or [""])[0], (q.get("key") or [""])[0])
            return self._send(200 if r else 404, r or {"error": "접수 번호 또는 확인 키가 맞지 않습니다"},
                              headers=self._cors())
        if path == "/api/public/project":
            slug = (parse_qs(u.query).get("p") or [""])[0]
            p = self.app[0].one("SELECT name FROM projects WHERE slug=?", (slug,))
            return self._send(200 if p else 404, {"name": p["name"]} if p else {"error": "없음"},
                              headers=self._cors())
        self._send(404, {"error": "없음"})

    def do_POST(self):
        m = re.fullmatch(r"/api/public/inquiries/([a-z0-9-]+)", urlparse(self.path).path)
        if not m:
            return self._send(404, {"error": "없음"})
        if not self._rate_ok():
            return self._send(429, {"error": "잠시 후 다시 시도하세요"}, headers=self._cors())
        db = self.app[0]
        proj = db.one("SELECT * FROM projects WHERE slug=?", (m.group(1),))
        if not proj:
            return self._send(404, {"error": "앱을 찾지 못했습니다"}, headers=self._cors())
        try:
            res = inquiries.submit(db, proj, self._json_body())
        except (inquiries.InquiryError, ValueError) as e:
            return self._send(400, {"error": str(e)}, headers=self._cors())
        self._send(200, res, headers=self._cors())


_servers = []
_on_shutdown = []


def _shutdown_all():
    time.sleep(0.3)
    for fn in _on_shutdown:
        fn()
    for s in _servers:
        s.shutdown()


def serve(db, sched, wf, block=True):
    s = settings.load()
    AdminHandler.app = PublicHandler.app = (db, sched, wf)
    admin = ThreadingHTTPServer(("127.0.0.1", s["ports"]["admin"]), AdminHandler)
    public = ThreadingHTTPServer((s["ports"]["public_bind"], s["ports"]["public"]), PublicHandler)
    preview = ThreadingHTTPServer(("127.0.0.1", s["ports"]["preview"]), PreviewHandler)
    admin.daemon_threads = public.daemon_threads = preview.daemon_threads = True
    _servers[:] = [admin, public, preview]
    threading.Thread(target=public.serve_forever, name="public", daemon=True).start()
    threading.Thread(target=preview.serve_forever, name="preview", daemon=True).start()
    if block:
        admin.serve_forever()
    else:
        threading.Thread(target=admin.serve_forever, name="admin", daemon=True).start()
    return admin, public
