"""5개 부서 설정 읽기와 프롬프트 구성. 전체 대화·전체 파일을 매번 보내지 않고
짧은 결정 기록과 필요한 자료만 보낸다."""
import json
from functools import lru_cache

from .paths import P

ORDER = ["planning", "design", "development", "qa", "release"]
FOLDERS = {"planning": "01-planning", "design": "02-design", "development": "03-development",
           "qa": "04-qa", "release": "05-release"}
NAMES = {"planning": "기획·고객 인사이트", "design": "디자인", "development": "개발",
         "qa": "품질 검증", "release": "출시·운영"}


def _dir(key):
    d = P.departments / FOLDERS[key]
    return d if d.exists() else P.repo_departments / FOLDERS[key]


@lru_cache(maxsize=None)
def config(key):
    return json.loads((_dir(key) / "config.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def role(key):
    return (_dir(key) / "role.md").read_text(encoding="utf-8")


def all_configs():
    return [config(k) for k in ORDER]


def data_block(label, value):
    """외부 자료(문의·첨부)는 지시가 아닌 자료로 감싼다."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    text = text.replace("</자료>", "</ 자료>")
    return f"<자료 이름=\"{label}\">\n{text}\n</자료>"


def build_prompt(task, sections, last_error=None):
    lines = [f"[작업] {task['kind']} — {task['title']}"]
    if task.get("done_criteria"):
        lines.append(f"[이 작업의 완료 기준] {task['done_criteria']}")
    for title, body in sections:
        if body in (None, "", [], {}):
            continue
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False, indent=1)
        lines.append(f"\n[{title}]\n{body}")
    if last_error:
        lines.append(f"\n[이전 시도의 오류 — 같은 실수를 반복하지 말 것]\n{last_error[:1500]}")
    lines.append("\n지정된 JSON 구조로만 답하라.")
    return "\n".join(lines)
