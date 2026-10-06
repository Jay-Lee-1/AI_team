"""앱 내 문의 접수와 고객/관리 화면 권한 분리."""
import base64
import json
import socket
import urllib.error
import urllib.request

from helpers import TeamCase

from aiteam import inquiries, server


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class InquiryTest(TeamCase):
    def setUp(self):
        super().setUp()
        self.pid = self.wf.create_project("shop", "작은 가게 예약 앱 아이디어입니다", demo=True)
        self.proj = self.wf.project(self.pid)

    def test_required_fields_and_duplicates(self):
        with self.assertRaises(inquiries.InquiryError):
            inquiries.submit(self.db, self.proj, {"category": "", "content": "내용입니다"})
        with self.assertRaises(inquiries.InquiryError):
            inquiries.submit(self.db, self.proj, {"category": "bug", "content": "짧"})
        a = inquiries.submit(self.db, self.proj, {"category": "bug", "content": "예약이 안 돼요", "anon_id": "A"})
        b = inquiries.submit(self.db, self.proj, {"category": "bug", "content": "예약이  안 돼요", "anon_id": "A"})
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["receipt_no"], b["receipt_no"])
        c = inquiries.submit(self.db, self.proj, {"category": "bug", "content": "결제도 안 돼요", "anon_id": "A"})
        self.assertFalse(c["duplicate"])
        row = self.db.one("SELECT * FROM inquiries WHERE receipt_no=?", (c["receipt_no"],))
        self.assertEqual(row["repeat_of_customer"], 1)  # 같은 고객의 반복 문의
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM inquiries")["n"], 2)

    def test_attachment_type_checked(self):
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nxxxx").decode()
        r = inquiries.submit(self.db, self.proj, {"category": "bug", "content": "화면이 깨져요",
                                                  "attachments": [{"name": "s.png", "data": png}]})
        self.assertTrue(r["receipt_no"])
        fake = base64.b64encode(b"MZ executable").decode()
        for name in ("s.png", "run.exe"):
            with self.assertRaises(inquiries.InquiryError):
                inquiries.submit(self.db, self.proj, {"category": "bug", "content": "첨부 다른 것",
                                                      "attachments": [{"name": name, "data": fake}]})

    def test_lookup_needs_key(self):
        r = inquiries.submit(self.db, self.proj, {"category": "usage", "content": "사용법 문의입니다"})
        self.assertIsNone(inquiries.lookup(self.db, r["receipt_no"], "wrong"))
        self.assertEqual(inquiries.lookup(self.db, r["receipt_no"], r["lookup_key"])["status"], "접수됨")

    def test_public_and_admin_separated(self):
        pa, pp, pv = _free_port(), _free_port(), _free_port()
        self.settings({})
        from aiteam import settings
        orig = settings.load

        def patched():
            s = orig()
            s["ports"] = {"admin": pa, "public": pp, "preview": pv, "public_bind": "127.0.0.1"}
            return s
        settings.load = patched
        try:
            admin, public = server.serve(self.db, self.s, self.wf, block=False)
            # 고객 서버: 문의 접수 가능, 관리 API 없음
            req = urllib.request.Request(f"http://127.0.0.1:{pp}/api/public/inquiries/{self.proj['slug']}",
                                         data=json.dumps({"category": "bug", "content": "고객 문의 테스트"}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as r:
                self.assertIn("receipt_no", json.loads(r.read()))
            for path in ("/api/state", "/api/control"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(f"http://127.0.0.1:{pp}{path}")
                self.assertEqual(e.exception.code, 404)
            # 관리 서버: 토큰 없으면 거부
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"http://127.0.0.1:{pa}/api/state")
            self.assertEqual(e.exception.code, 401)
            req = urllib.request.Request(f"http://127.0.0.1:{pa}/api/control", data=b'{"action":"start"}',
                                         headers={"Cookie": f"aiteam_admin={server.admin_token()}"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as e:  # CSRF 방지 헤더 없음
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 401)
            req = urllib.request.Request(f"http://127.0.0.1:{pa}/api/state",
                                         headers={"Cookie": f"aiteam_admin={server.admin_token()}"})
            with urllib.request.urlopen(req) as r:
                st = json.loads(r.read())
            self.assertEqual(st["inquiries"][0]["content"], "고객 문의 테스트")
            self.assertNotIn("customer_key", json.dumps(st))
        finally:
            settings.load = orig
            for srv in server._servers:
                srv.shutdown()
                srv.server_close()
