"""부서별 출력 규약(JSON 스키마)과 최소 검증기.
CLI가 스키마로 구조화 출력을 강제하고, 여기서 한 번 더 검사한다."""


def S(desc=None, max_len=None):
    d = {"type": "string"}
    if desc:
        d["description"] = desc
    if max_len:
        d["maxLength"] = max_len
    return d


def A(items, max_items=None):
    d = {"type": "array", "items": items}
    if max_items:
        d["maxItems"] = max_items
    return d


def O(props, required=None):
    return {"type": "object", "properties": props,
            "required": list(props) if required is None else required,
            "additionalProperties": False}


B = {"type": "boolean"}
I = {"type": "integer"}
E = lambda *v: {"type": "string", "enum": list(v)}  # noqa: E731

USER_ACTION = O({
    "todo": S("사용자가 할 일"), "why_now": S("지금 필요한 이유"),
    "where": S("공식 사이트 또는 설정 위치"), "steps": A(S()),
    "cost": O({"value": S(), "kind": E("confirmed", "estimate", "unknown")}),
    "report_back": S("완료 후 알려줄 내용(비밀정보 제외)"),
    "blocked_work": S("이 일 때문에 대기하는 작업"), "meanwhile": S("그동안 팀이 진행할 일"),
})
UA = A(USER_ACTION, 5)

DESIGN = O({
    "summary": S(),
    "design_system": O({
        "colors": A(O({"name": S(), "value": S(), "usage": S()})),
        "typography": A(O({"name": S(), "size": S(), "weight": S(), "usage": S()})),
        "spacing": A(S()),
        "components": A(O({"name": S(), "spec": S()})),
    }),
    "user_flows": A(O({"name": S(), "steps": A(S())})),
    "screens": A(O({"name": S(), "purpose": S(), "elements": A(S()),
                    "states": O({"loading": S(), "error": S(), "empty": S()})})),
    "accessibility": A(S()), "responsive": A(S()),
    "preview_html": S("실제 문구와 데이터 예시가 있는 정적 HTML 미리보기(스크립트 없이)"),
    "needs_direction_choice": B,
    "direction_options": A(O({"id": S(), "title": S(), "description": S(),
                              "preview_html": S()}), 2),
    "handoff_notes": A(S()),
    "user_actions": UA,
})

SCHEMAS = {
    "planning.define_mvp": O({
        "summary": S(),
        "target_customers": A(O({"segment": S(), "description": S()})),
        "problems": A(O({"problem": S(), "basis": E("hypothesis", "confirmed"), "source": S()})),
        "core_features": A(O({"name": S(), "description": S(),
                              "priority": E("must", "should", "could")})),
        "mvp_scope": O({"included": A(S()), "excluded": A(S())}),
        "done_criteria": A(S()),
        "directions": A(O({"id": S(), "title": S(), "description": S(), "tradeoffs": S(),
                           "recommended": B}), 3),
        "questions": A(O({"question": S(), "why": S(), "options": A(S()),
                          "recommendation": S()}), 3),
        "tech_recommendation": O({"platform": S(), "stack": S(), "reason": S()}),
        "user_actions": UA,
    }),
    "planning.revise": O({"summary": S(), "updated_done_criteria": A(S()),
                          "clarifications": A(S())}),
    "planning.analyze_inquiries": O({
        "assignments": A(O({"inquiry_id": I, "cluster": S("기존 묶음 'C번호' 또는 새 묶음 키")})),
        "clusters": A(O({
            "ref": S(), "title": S(), "problem": S(),
            "customer_words": A(S("고객이 실제로 쓴 표현 인용")),
            "requested_feature": S("고객이 요청한 기능"),
            "underlying_problem": S("그 기능으로 해결하려는 문제"),
            "kind": E("bug", "feature", "usability", "question", "other"),
            "severity": E("critical", "high", "medium", "low"),
            "impact": S(), "reproducible": B,
            "facts": A(S("문의 원문·재현 정보로 확인된 사실")),
            "estimates": A(S("팀의 추정")), "reply_draft": S("고객 답변 초안(자동 전송 안 함)"),
        })),
    }),
    "planning.propose_improvements": O({
        "proposals": A(O({
            "title": S(), "problem": S(), "evidence_cluster_refs": A(S()),
            "expected_outcome": S(), "change": S(), "alternatives": A(S()),
            "effect_and_measure": S(), "size": E("small", "medium", "large"),
            "cost_and_uncertainty": S(), "external_setup_and_risks": S(),
            "recommendation_reason": S(),
        }), 3),
        "notes": S(),
    }),
    "planning.strengthen_proposal": O({
        "additional_evidence": A(S()), "open_questions": A(S()),
        "revised_recommendation": S(),
    }),
    "design.initial": DESIGN,
    "design.improvement": DESIGN,
    "design.revise": DESIGN,
    "development.implement": O({
        "summary": S(),
        "changes": A(O({"path": S("작업 사본 기준 상대 경로"),
                        "op": E("create", "replace", "edit"),
                        "content": S("create/replace일 때 전체 내용, edit이면 빈 문자열"),
                        "edits": A(O({"old": S(), "new": S()}))})),
        "connections": A(O({"feature": S(), "kind": E("mock", "real", "none"), "note": S()})),
        "proposed_checks": A(S()),
        "work_complete": B,
        "remaining_subtasks": A(S(), 6),
        "notes_for_qa": A(S()),
        "user_actions": UA,
    }),
    "qa.verify": O({
        "verdict": E("pass", "fail"), "summary": S(),
        "findings": A(O({"severity": E("critical", "high", "medium", "low"),
                         "area": E("planning", "design", "development"),
                         "description": S(), "evidence": S()})),
        "verified_features": A(S()), "untested": A(S()),
    }),
    "qa.usability_check": O({
        "summary": S(),
        "findings": A(O({"severity": E("critical", "high", "medium", "low"),
                         "description": S(), "evidence": S(), "suggestion": S()})),
    }),
    "release.prepare": O({
        "store_listing": O({"name": S(), "tagline": S(), "description": S(),
                            "features": A(S())}),
        "user_guide_md": S(), "release_notes": S(),
        "deploy_plan": A(S()), "rollback_plan": A(S()),
        "user_actions": UA,
    }),
    "release.docs": O({"user_guide_md": S(), "faq": A(O({"q": S(), "a": S()}))}),
}


class ContractError(Exception):
    pass


def validate(value, schema, path="$"):
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise ContractError(f"{path}: 객체가 아닙니다")
        for k in schema.get("required", []):
            if k not in value:
                raise ContractError(f"{path}.{k}: 필수 항목 누락")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")
    elif t == "array":
        if not isinstance(value, list):
            raise ContractError(f"{path}: 배열이 아닙니다")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ContractError(f"{path}: 최대 {schema['maxItems']}개")
        for i, v in enumerate(value):
            validate(v, schema["items"], f"{path}[{i}]")
    elif t == "string":
        if not isinstance(value, str):
            raise ContractError(f"{path}: 문자열이 아닙니다")
        if "enum" in schema and value not in schema["enum"]:
            raise ContractError(f"{path}: 허용 값 아님 ({value})")
    elif t == "boolean":
        if not isinstance(value, bool):
            raise ContractError(f"{path}: 참/거짓이 아닙니다")
    elif t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ContractError(f"{path}: 정수가 아닙니다")
    return value


def check(kind, output):
    schema = SCHEMAS[kind]
    # 최대 개수 초과는 거부 대신 잘라낸다(예: 개선안 최대 3개)
    _truncate(output, schema)
    return validate(output, schema)


def _truncate(value, schema):
    if schema.get("type") == "object" and isinstance(value, dict):
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                _truncate(value[k], sub)
    elif schema.get("type") == "array" and isinstance(value, list):
        if "maxItems" in schema:
            del value[schema["maxItems"]:]
        for v in value:
            _truncate(v, schema["items"])


def for_cli(schema):
    """CLI 전달용: 개수·길이 제한은 코드에서 강제하므로 제외한다."""
    if isinstance(schema, dict):
        return {k: for_cli(v) for k, v in schema.items() if k not in ("maxItems", "maxLength")}
    if isinstance(schema, list):
        return [for_cli(v) for v in schema]
    return schema
