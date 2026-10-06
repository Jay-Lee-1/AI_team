"""앱 내 고객 문의 접수·중복 검사·집계(일반 코드)."""
import base64
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from .db import jd, jl, now
from .paths import P
from .safety import SafetyError, safe_join

CATEGORIES = {"bug": "오류 신고", "feature": "기능 요청", "usage": "사용 방법",
              "billing": "결제·계정", "other": "기타"}
ALLOWED_ATTACH = {".png": b"\x89PNG", ".jpg": b"\xff\xd8", ".jpeg": b"\xff\xd8",
                  ".webp": b"RIFF", ".txt": None}
MAX_ATTACH = 2 * 1024 * 1024
DUP_WINDOW_MIN = 10


class InquiryError(Exception):
    pass


def _salt():
    if not P.salt_file.exists():
        P.salt_file.write_text(secrets.token_hex(16), encoding="utf-8")
    return P.salt_file.read_text(encoding="utf-8").strip()


def customer_key(anon_id):
    anon = (anon_id or "").strip()[:100] or secrets.token_hex(8)
    return hashlib.sha256((_salt() + anon).encode()).hexdigest()[:20]


def _clean(s, n):
    s = (s or "").replace("\x00", "").strip()
    return s[:n]


def submit(db, project, data):
    """고객 문의 접수. 필수: 분류·내용. 선택: 기대 결과·연락처·첨부."""
    category = data.get("category")
    content = _clean(data.get("content"), 4000)
    if category not in CATEGORIES:
        raise InquiryError("문의 분류를 선택하세요")
    if len(content) < 5:
        raise InquiryError("문의 내용을 5자 이상 적어 주세요")
    expected = _clean(data.get("expected"), 1000)
    contact = _clean(data.get("contact"), 200)
    ckey = customer_key(data.get("anon_id"))
    norm = re.sub(r"\s+", " ", content.lower())
    dedup = hashlib.sha256(f"{ckey}|{category}|{norm}".encode()).hexdigest()
    since = (datetime.now(timezone.utc) - timedelta(minutes=DUP_WINDOW_MIN)).isoformat()
    dup = db.one("SELECT receipt_no FROM inquiries WHERE dedup_hash=? AND created_at>=?",
                 (dedup, since))
    if dup:
        return {"receipt_no": dup["receipt_no"], "duplicate": True}
    repeat = db.one("SELECT COUNT(*) n FROM inquiries WHERE project_id=? AND customer_key=?",
                    (project["id"], ckey))["n"]
    receipt = f"Q-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
    lookup = secrets.token_urlsafe(9)
    atts = _save_attachments(project, receipt, data.get("attachments") or [])
    db.insert("inquiries", project_id=project["id"], receipt_no=receipt,
              lookup_hash=hashlib.sha256(lookup.encode()).hexdigest(), category=category,
              content=content, expected=expected,
              app_version=_clean(data.get("app_version"), 40),
              screen=_clean(data.get("screen"), 200), device=_clean(data.get("device"), 120),
              contact=contact, attachments_json=jd(atts), customer_key=ckey, dedup_hash=dedup,
              repeat_of_customer=int(repeat > 0), status="received",
              source=_clean(data.get("source"), 20) or "app", created_at=now())
    db.event("inquiry", f"새 고객 문의 접수 {receipt} ({CATEGORIES[category]})",
             project_id=project["id"], dept="release", is_mock=bool(project["is_demo"]))
    return {"receipt_no": receipt, "lookup_key": lookup, "duplicate": False}


def _save_attachments(project, receipt, items):
    saved = []
    base = P.inbox_dir(project["slug"]) / "attachments"
    base.mkdir(parents=True, exist_ok=True)
    for i, it in enumerate(items[:2]):
        name = str(it.get("name") or "")
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext not in ALLOWED_ATTACH:
            raise InquiryError("첨부는 png, jpg, webp, txt만 가능합니다")
        try:
            raw = base64.b64decode(it.get("data") or "", validate=True)
        except ValueError:
            raise InquiryError("첨부 파일을 읽지 못했습니다")
        if len(raw) > MAX_ATTACH:
            raise InquiryError("첨부는 2MB 이하만 가능합니다")
        magic = ALLOWED_ATTACH[ext]
        if magic and not raw.startswith(magic):
            raise InquiryError("첨부 형식이 확장자와 다릅니다")
        fname = f"{receipt}-{i + 1}{ext}"
        try:
            target = safe_join(base, fname)
        except SafetyError as e:
            raise InquiryError(str(e))
        target.write_bytes(raw)
        saved.append({"file": fname, "original": _clean(name, 80), "size": len(raw)})
    return saved


def lookup(db, receipt, key):
    row = db.one("SELECT receipt_no,category,status,created_at,lookup_hash FROM inquiries "
                 "WHERE receipt_no=?", (receipt,))
    if not row or not key or hashlib.sha256(key.encode()).hexdigest() != row["lookup_hash"]:
        return None
    return {"receipt_no": row["receipt_no"], "category": CATEGORIES.get(row["category"]),
            "status": STATUS_KO.get(row["status"], row["status"]), "created_at": row["created_at"]}


STATUS_KO = {"received": "접수됨", "analyzed": "검토 중", "in_progress": "개선 진행 중",
             "deployed": "개선 배포 완료(해결 확인 전)", "resolved_confirmed": "해결 확인됨",
             "closed": "종료"}


def cluster_stats(db, cluster_id):
    rows = db.all("SELECT customer_key, created_at FROM inquiries WHERE cluster_id=?",
                  (cluster_id,))
    nowdt = datetime.now(timezone.utc)
    last7 = prev7 = 0
    for r in rows:
        try:
            age = nowdt - datetime.fromisoformat(r["created_at"])
        except ValueError:
            continue
        if age <= timedelta(days=7):
            last7 += 1
        elif age <= timedelta(days=14):
            prev7 += 1
    trend = "증가" if last7 > prev7 else ("감소" if last7 < prev7 else "유지")
    return {"count": len(rows), "customers": len({r["customer_key"] for r in rows}),
            "last7": last7, "prev7": prev7, "trend": trend}


def import_inbox_files(db, project):
    """inbox/<프로젝트>/import/*.json 파일(다른 채널에서 모은 문의)을 가져온다."""
    import json
    folder = P.inbox_dir(project["slug"]) / "import"
    if not folder.exists():
        return 0
    n = 0
    done = folder / "imported"
    done.mkdir(exist_ok=True)
    for f in sorted(folder.glob("*.json")):
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
            for it in items if isinstance(items, list) else [items]:
                it = dict(it)
                it.pop("attachments", None)
                it["source"] = "import"
                try:
                    submit(db, project, it)
                    n += 1
                except InquiryError:
                    continue
            f.replace(done / f.name)
        except (OSError, ValueError):
            db.event("error", f"문의 가져오기 실패: {f.name}", project_id=project["id"])
    return n


def jl_safe(s):
    return jl(s, [])
