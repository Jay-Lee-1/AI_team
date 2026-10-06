"""모의 호출 결과. 실제 모델 없이 흐름을 시험하기 위한 것이며 화면에 '모의'로 표시된다.
개발 결과는 실제 파일로 적용되고 실제 검증 명령으로 확인된다."""
from .paths import P

UA = []


def _design(inp, revise=False):
    return {
        "summary": "[모의] 한 화면 중심의 할 일 앱 디자인",
        "design_system": {
            "colors": [{"name": "primary", "value": "#3559E0", "usage": "주요 버튼"},
                       {"name": "text", "value": "#1F2430", "usage": "본문"},
                       {"name": "surface", "value": "#FFFFFF", "usage": "카드 배경"}],
            "typography": [{"name": "body", "size": "16px", "weight": "400", "usage": "본문"},
                           {"name": "title", "size": "22px", "weight": "700", "usage": "제목"}],
            "spacing": ["4px", "8px", "16px", "24px"],
            "components": [{"name": "버튼", "spec": "높이 44px, 모서리 10px"},
                           {"name": "입력창", "spec": "높이 44px, 레이블 항상 표시"}]},
        "user_flows": [{"name": "할 일 추가", "steps": ["입력", "추가 버튼", "목록에 표시"]}],
        "screens": [{"name": "오늘의 할 일", "purpose": "할 일 추가·완료",
                     "elements": ["제목", "입력창", "추가 버튼", "목록", "문의하기 버튼"],
                     "states": {"loading": "불러오는 중 문구", "error": "저장 실패 안내와 다시 시도",
                                "empty": "아직 할 일이 없어요 안내"}}],
        "accessibility": ["입력창 레이블", "대비 4.5:1 이상", "키보드로 추가 가능"],
        "responsive": ["360px 폭에서 한 열"],
        "preview_html": "<!doctype html><meta charset='utf-8'><title>미리보기</title>"
                        "<style>body{font-family:sans-serif;max-width:420px;margin:24px auto;color:#1F2430}"
                        "button{background:#3559E0;color:#fff;border:0;border-radius:10px;height:44px;padding:0 16px}"
                        "li{padding:12px;border-bottom:1px solid #eee}</style>"
                        "<p style='color:#b45309'>[모의 미리보기]</p><h1>오늘의 할 일</h1>"
                        "<label>할 일<br><input value='우유 사기'></label> <button>추가</button>"
                        "<ul><li>☐ 운동 30분</li><li>☑ 보고서 제출</li></ul><p>아직 할 일이 없어요 (빈 화면 예시)</p>"
                        "<button>문의하기</button>",
        "needs_direction_choice": False, "direction_options": [],
        "handoff_notes": ["[모의] 위젯 스크립트로 문의하기 버튼 연결"], "user_actions": UA,
    }


APP_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>오늘의 할 일 (모의 데모)</title><link rel="stylesheet" href="style.css"></head>
<body><main>
<p class="demo">모의 데모 앱입니다</p>
<h1>오늘의 할 일</h1>
<form id="f"><label for="t">할 일</label><input id="t" required><button>추가</button></form>
<ul id="list"></ul><p id="empty">아직 할 일이 없어요</p>
</main>
<script src="app.js"></script>
WIDGET
</body></html>
"""
APP_JS = """const list=document.getElementById('list');const empty=document.getElementById('empty');
let items=JSON.parse(localStorage.getItem('todo')||'[]');
function draw(){list.innerHTML='';items.forEach((t,i)=>{const li=document.createElement('li');
li.textContent=t;list.appendChild(li);});empty.hidden=items.length>0;}
document.getElementById('f').onsubmit=e=>{e.preventDefault();const v=document.getElementById('t').value.trim();
if(v){items.push(v);localStorage.setItem('todo',JSON.stringify(items));draw();e.target.reset();}};draw();
"""
APP_CSS = """body{font-family:system-ui,'Malgun Gothic',sans-serif;max-width:480px;margin:0 auto;padding:16px;color:#1F2430}
.demo{color:#b45309}input{height:44px;font-size:16px}button{height:44px;background:#3559E0;color:#fff;border:0;border-radius:10px;padding:0 16px}
"""
TEST_PY = """import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class AppTest(unittest.TestCase):
    def test_has_title_and_form(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("오늘의 할 일", html)
        self.assertIn('<label for="t">', html)

    def test_inquiry_widget_connected(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("widget.js", html)

    def test_empty_state(self):
        self.assertIn("아직 할 일이 없어요", (ROOT / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
"""


def generate(kind, inp):
    if kind == "planning.define_mvp":
        return {
            "summary": "[모의] 바쁜 직장인이 오늘 할 일을 빠르게 적고 끝내는 앱",
            "target_customers": [{"segment": "직장인", "description": "출근 전 할 일을 정리하는 사람"}],
            "problems": [{"problem": "할 일을 적을 곳이 흩어져 있다", "basis": "hypothesis",
                          "source": "사용자 아이디어"}],
            "core_features": [{"name": "할 일 추가", "description": "한 줄 입력", "priority": "must"},
                              {"name": "문의하기", "description": "앱 안에서 문의 접수", "priority": "must"}],
            "mvp_scope": {"included": ["할 일 추가·목록", "빈 화면 안내", "문의하기"],
                          "excluded": ["로그인", "동기화", "결제"]},
            "done_criteria": ["할 일을 추가하면 목록에 보인다", "할 일이 없으면 빈 화면 안내가 보인다",
                              "앱 안에서 문의하기 버튼이 보인다"],
            "directions": [{"id": "A", "title": "한 화면 단순형", "description": "입력과 목록만",
                            "tradeoffs": "빠르지만 기능 적음", "recommended": True},
                           {"id": "B", "title": "카테고리형", "description": "업무/개인 구분",
                            "tradeoffs": "유용하지만 개발 큼", "recommended": False}],
            "questions": [{"question": "할 일을 기기 사이에 동기화해야 하나요?",
                           "why": "필요하면 계정·서버가 필요해 범위가 커집니다",
                           "options": ["지금은 필요 없음", "필요"], "recommendation": "지금은 필요 없음"}],
            "tech_recommendation": {"platform": "웹 앱(PWA)", "stack": "HTML/CSS/JS",
                                    "reason": "설치 없이 바로 검증 가능"},
            "user_actions": UA,
        }
    if kind == "planning.revise":
        return {"summary": "[모의] 기획 보완", "updated_done_criteria": [], "clarifications": ["[모의] 보완"]}
    if kind in ("design.initial", "design.improvement", "design.revise"):
        return _design(inp)
    if kind == "development.implement":
        ws = P.workspace(inp["_project"])
        if not (ws / "index.html").exists():
            from . import settings
            port = settings.load()["ports"]["public"]
            widget = (f'<script src="http://127.0.0.1:{port}/widget.js" data-project="{inp["_project"]}" '
                      f'data-version="0.1.0" defer></script>')
            changes = [{"path": "index.html", "op": "create", "content": APP_HTML.replace("WIDGET", widget), "edits": []},
                       {"path": "app.js", "op": "create", "content": APP_JS, "edits": []},
                       {"path": "style.css", "op": "create", "content": APP_CSS, "edits": []},
                       {"path": "tests/test_app.py", "op": "create", "content": TEST_PY, "edits": []}]
        else:
            note = (inp.get("proposal") or {}).get("title") or inp.get("subtask") or "수정"
            prev = (ws / "CHANGELOG.md").read_text(encoding="utf-8") if (ws / "CHANGELOG.md").exists() else "# 변경 기록\n"
            changes = [{"path": "CHANGELOG.md", "op": "replace", "content": prev + f"- [모의] {note}\n", "edits": []}]
        return {"summary": "[모의] 화면과 로컬 저장 구현", "changes": changes,
                "connections": [{"feature": "할 일 저장", "kind": "none", "note": "브라우저 저장소(로컬)"},
                                {"feature": "문의하기", "kind": "real", "note": "로컬 문의 접수 서버"}],
                "proposed_checks": ["python -m unittest discover -s tests"], "work_complete": True,
                "remaining_subtasks": [], "notes_for_qa": ["[모의] 빈 화면 확인"], "user_actions": UA}
    if kind == "qa.verify":
        crit = (inp.get("_plan") or {}).get("done_criteria") or ["기본 화면"]
        return {"verdict": "pass", "summary": "[모의] 완료 기준과 테스트 결과 대조",
                "findings": [], "verified_features": crit, "untested": ["실제 기기 화면"]}
    if kind == "qa.usability_check":
        return {"summary": "[모의] 점검", "findings": [{"severity": "low", "description": "버튼 대비 확인",
                                                      "evidence": "style.css", "suggestion": "유지"}]}
    if kind == "release.prepare":
        feats = inp.get("verified_features") or []
        return {"store_listing": {"name": "오늘의 할 일(모의)", "tagline": "[모의] 할 일을 빠르게",
                                  "description": "[모의] 검증된 기능만 소개합니다",
                                  "features": feats + ["AI 자동 일정 추천"]},
                "user_guide_md": "# 사용 안내 (모의)\n1. 할 일을 입력하고 추가를 누릅니다.\n2. 문제가 있으면 '문의하기'를 누릅니다.",
                "release_notes": f"[모의] v{inp.get('version')} 변경 사항",
                "deploy_plan": ["[모의] 정적 파일을 호스팅에 올린다"],
                "rollback_plan": ["[모의] 이전 커밋의 파일을 다시 올린다"], "user_actions": UA}
    if kind == "release.docs":
        return {"user_guide_md": "# 사용 안내 보완 (모의)", "faq": [{"q": "저장은?", "a": "이 기기에만"}]}
    if kind == "planning.analyze_inquiries":
        return {"assignments": [{"inquiry_id": i, "cluster": "N1"} for i in inp["inquiry_ids"]],
                "clusters": [{"ref": "N1", "title": "[모의] 할 일 수정 기능 요청", "problem": "잘못 적은 할 일을 고칠 수 없다",
                              "customer_words": ["수정이 안 돼요"], "requested_feature": "할 일 수정 버튼",
                              "underlying_problem": "오타를 고치려면 지우고 다시 써야 한다",
                              "kind": "feature", "severity": "medium", "impact": "[모의] 매일 쓰는 사용자 불편",
                              "reproducible": True, "facts": ["수정 기능 없음(코드 확인)"],
                              "estimates": ["다른 고객도 겪을 수 있음(추정)"],
                              "reply_draft": "[모의] 의견 감사합니다. 검토 중입니다."}]}
    if kind == "planning.propose_improvements":
        refs = [c["ref"] for c in inp["clusters"]][:3]
        return {"proposals": [{"title": f"[모의] {c['title']} 해결", "problem": c["problem"],
                               "evidence_cluster_refs": [c["ref"]], "expected_outcome": "오타를 바로 고친다",
                               "change": "목록 항목 길게 눌러 수정", "alternatives": ["삭제 후 재입력 안내"],
                               "effect_and_measure": "관련 문의 감소로 확인", "size": "small",
                               "cost_and_uncertainty": "작음/낮음", "external_setup_and_risks": "없음",
                               "recommendation_reason": "작고 문의가 반복됨"}
                              for c in inp["clusters"] if c["ref"] in refs], "notes": "[모의]"}
    if kind == "planning.strengthen_proposal":
        return {"additional_evidence": ["[모의] 근거"], "open_questions": [], "revised_recommendation": "유지"}
    raise ValueError(kind)
