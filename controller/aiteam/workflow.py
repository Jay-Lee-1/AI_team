"""작업 흐름: 1 기획 → 2 디자인 → 3 개발 → 4 검증 → 5 출시 → (문의) → 1 …
상태 전환·중복 검사·승인 게이트는 모두 일반 코드로 처리한다."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from . import budget, checks, contracts, departments, gitops, inquiries, settings
from .db import jd, jl, now
from .paths import P
from .safety import SafetyError, assert_inside, guarded_write, safe_join

DEPT_OF = {"planning": "planning", "design": "design", "development": "development",
           "qa": "qa", "release": "release"}

# 우선순위: 1 승인된 개선·초기 개발, 2 유지보수 버그, 3 문의 분석, 4 근거 보강, 5 점검, 6 자료
PRIORITY = {
    "planning.define_mvp": 1, "planning.revise": 1, "design.initial": 1, "design.improvement": 1,
    "design.revise": 1, "development.implement": 1, "qa.verify": 1, "release.prepare": 1,
    "planning.analyze_inquiries": 3, "planning.propose_improvements": 3,
    "planning.strengthen_proposal": 4, "qa.usability_check": 5, "release.docs": 6,
}
WRITE_KINDS = {"development.implement", "qa.verify", "qa.usability_check"}
DECISION_DEPT = {"mvp_direction": "planning", "design_direction": "design",
                 "proposal_selection": "planning", "deploy_approval": "release",
                 "qa_escalation": "qa", "user_action": None}
MAX_DEV_CHAIN = 8
MAX_QA_FAILS = 5


class GateError(Exception):
    pass


class ApplyError(Exception):
    pass


def _key(*parts):
    return hashlib.sha256("|".join(json.dumps(p, ensure_ascii=False, sort_keys=True)
                                   for p in parts).encode()).hexdigest()[:32]


class Workflow:
    def __init__(self, db):
        self.db = db

    # ───────────── 공통 ─────────────
    def project(self, pid):
        return self.db.one("SELECT * FROM projects WHERE id=?", (pid,))

    def cycle(self, cid):
        return self.db.one("SELECT * FROM cycles WHERE id=?", (cid,)) if cid else None

    def psettings(self, project):
        return jl(project["settings_json"], {}) or {}

    def set_psettings(self, project, patch):
        cur = self.psettings(project)
        cur.update(patch)
        self.db.update("projects", project["id"], settings_json=jd(cur))

    def create_project(self, name, idea, customers="", source_path="", demo=False):
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:30] or "app"
        base = slug
        i = 2
        while self.db.one("SELECT 1 FROM projects WHERE slug=?", (slug,)):
            slug = f"{base}-{i}"
            i += 1
        if demo:
            slug = slug if slug.startswith("demo") else f"demo-{slug}"[:30]
        pid = self.db.insert("projects", slug=slug, name=name, goal=idea[:200], idea=idea,
                             customers=customers, source_path=source_path or None,
                             is_demo=int(demo), created_at=now(), stage="기획 중",
                             settings_json=jd({"checks": [], "deploy_command": "",
                                               "preview_path": "index.html"}))
        for d in (P.project_dir(slug), P.artifact_dir(slug), P.inbox_dir(slug)):
            d.mkdir(parents=True, exist_ok=True)
        cid = self.db.insert("cycles", project_id=pid, kind="initial", approved=0,
                             created_at=now())
        self.db.event("project", f"프로젝트 생성: {name}" + (" [모의 데모]" if demo else ""),
                      project_id=pid, is_mock=demo)
        self.create_task(pid, "planning.define_mvp", "아이디어로 최소 제품 범위·완료 기준 정하기",
                         {"idea": idea, "customers": customers}, cycle_id=cid,
                         done_criteria="고객·문제·핵심 기능·최소 범위·완료 기준·방향 선택지가 정리됨")
        return pid

    def create_task(self, pid, kind, title, inp, cycle_id=None, priority=None,
                    is_fallback=False, code_version="", done_criteria=None):
        dept = DEPT_OF[kind.split(".")[0]]
        if kind == "development.implement":
            cyc = self.cycle(cycle_id)
            if not cyc or not cyc["approved"]:
                self.db.event("blocked", "승인(선택)되지 않은 작업의 개발 요청을 차단했습니다",
                              project_id=pid, dept="development")
                raise GateError("사용자가 선택·승인하지 않은 작업은 개발할 수 없습니다")
        key = _key(pid, kind, inp, code_version, cycle_id)
        existing = self.db.one("SELECT id FROM tasks WHERE dedup_key=? AND status NOT IN "
                               "('failed','cancelled')", (key,))
        if existing:
            return existing["id"]
        cfg = departments.config(dept)
        try:
            tid = self.db.insert(
                "tasks", project_id=pid, cycle_id=cycle_id, dept=dept, kind=kind, title=title,
                input_json=jd(inp), dedup_key=key, status="queued",
                priority=priority or PRIORITY.get(kind, 3), is_fallback=int(is_fallback),
                token_limit=cfg.get("token_limit"), time_limit_sec=cfg.get("timeout_sec"),
                done_criteria=done_criteria, created_at=now())
        except Exception:  # 동시 생성 경쟁 → 기존 작업 재사용
            row = self.db.one("SELECT id FROM tasks WHERE dedup_key=?", (key,))
            if row:
                return row["id"]
            raise
        self.db.event("queued", f"작업 대기열 추가: {title}", project_id=pid, dept=dept,
                      task_id=tid, is_mock=self._is_mock(pid))
        return tid

    def _is_mock(self, pid):
        p = self.project(pid)
        return bool(p and p["is_demo"]) or settings.load()["runner"] == "mock"

    def handoff(self, task, to_dept, message):
        self.db.event("handoff", f"{departments.NAMES[task['dept']]} → "
                                 f"{departments.NAMES[to_dept]}: {message}",
                      project_id=task["project_id"], dept=to_dept, task_id=task["id"],
                      is_mock=self._is_mock(task["project_id"]))
        self.db.update("tasks", task["id"], next_dept=to_dept)

    def add_decision(self, pid, kind, title, body, dedup, cycle_id=None, blocks=""):
        if self.db.one("SELECT 1 FROM decisions WHERE dedup_key=?", (dedup,)):
            return None  # 같은 결정 요청을 반복하지 않는다
        did = self.db.insert("decisions", project_id=pid, cycle_id=cycle_id, kind=kind,
                             title=title, body_json=jd(body), status="pending",
                             dedup_key=dedup, blocks=blocks, requested_at=now())
        self.db.event("decision", f"사용자 결정 요청: {title}", project_id=pid,
                      dept=DECISION_DEPT.get(kind), is_mock=self._is_mock(pid))
        return did

    def add_user_actions(self, task, actions):
        for a in actions or []:
            self.add_decision(task["project_id"], "user_action", a["todo"], a,
                              _key("ua", task["project_id"], a["todo"]),
                              cycle_id=task["cycle_id"], blocks=a.get("blocked_work", ""))

    def save_artifact(self, task, rel, content, title, kind="doc"):
        proj = self.project(task["project_id"])
        base = P.artifact_dir(proj["slug"])
        base.mkdir(parents=True, exist_ok=True)
        path = guarded_write(base, rel, content, [P.artifacts])
        self.db.insert("artifacts", project_id=proj["id"], task_id=task["id"], dept=task["dept"],
                       kind=kind, path=str(path.relative_to(P.artifacts)).replace("\\", "/"),
                       title=title, created_at=now())
        return path

    def decision_log(self, pid, limit=8):
        rows = self.db.all("SELECT title, answer_json FROM decisions WHERE project_id=? AND "
                           "status IN ('answered','done') AND kind!='user_action' "
                           "ORDER BY answered_at DESC LIMIT ?", (pid, limit))
        return [f"- {r['title']}: {(r['answer_json'] or '')[:300]}" for r in rows]

    # ───────────── 프롬프트 구성 ─────────────
    def build_spec(self, task):
        proj = self.project(task["project_id"])
        cyc = self.cycle(task["cycle_id"])
        inp = jl(task["input_json"], {})
        kind = task["kind"]
        dept = task["dept"]
        s = settings.load()
        cfg = departments.config(dept)
        common = [("프로젝트", f"{proj['name']} / 목표: {proj['goal']}"),
                  ("짧은 결정 기록", "\n".join(self.decision_log(proj["id"])))]
        sections = list(common)
        ws = P.workspace(proj["slug"])
        cwd = P.temp
        tools = []
        plan = jl(cyc["plan_json"], {}) if cyc else {}
        design = jl(cyc["design_json"], {}) if cyc else {}

        if kind == "planning.define_mvp":
            sections += [("아이디어", departments.data_block("사용자 아이디어", inp["idea"])),
                         ("주요 고객(사용자 입력)", inp.get("customers")),
                         ("첨부자료", self._inbox_docs(proj)),
                         ("기존 앱", "있음: 작업 사본의 기술 구성을 우선 활용" if
                          proj["source_path"] else "없음(새로 만듦)")]
        elif kind == "planning.revise":
            sections += [("현재 기획", plan), ("검증 반려 사유", inp.get("findings"))]
        elif kind == "planning.analyze_inquiries":
            rows = self.db.all(
                "SELECT id,category,content,expected,app_version,screen,device,created_at, "
                "repeat_of_customer FROM inquiries WHERE id IN (%s)" %
                ",".join("?" * len(inp["inquiry_ids"])), tuple(inp["inquiry_ids"]))
            clusters = self.db.all("SELECT id,title,problem,kind FROM clusters WHERE project_id=? "
                                   "AND status!='resolved_confirmed'", (proj["id"],))
            sections += [("새 고객 문의", departments.data_block("고객 문의 원본", rows)),
                         ("기존 문제 묶음(참조는 'C번호')",
                          [{"ref": f"C{c['id']}", **c} for c in clusters])]
        elif kind == "planning.propose_improvements":
            sections += [("문제 묶음과 코드로 계산한 집계", inp["clusters"]),
                         ("기존 개선안(중복 제안 금지)", inp.get("existing")),
                         ("현재 앱 범위", plan.get("mvp_scope"))]
        elif kind == "planning.strengthen_proposal":
            sections += [("개선안", inp["proposal"]),
                         ("근거 문의 원본", departments.data_block("고객 문의 원본", inp["inquiries"]))]
        elif kind.startswith("design."):
            prev = self._latest_design_system(proj["id"])
            sections += [("승인된 기획", plan), ("기존 디자인 체계(일관성 유지)", prev)]
            if kind == "design.improvement":
                sections.append(("선택된 개선안과 실행 범위", inp.get("proposal")))
            if kind == "design.revise":
                sections += [("현재 디자인", design), ("검증 반려 사유", inp.get("findings"))]
            sections.append(("문의하기 진입점", "앱 화면 안에 '문의하기' 버튼(위젯)을 둔다"))
        elif kind == "development.implement":
            gitops.ensure_repo(ws)
            files, total = gitops.ls_files(ws)
            cwd, tools = ws, cfg["permissions"]["model_tools"]
            design_brief = {k: design.get(k) for k in ("design_system", "user_flows", "screens",
                                                        "accessibility", "responsive",
                                                        "chosen_direction")}
            sections += [("승인된 기획", {k: plan.get(k) for k in
                                       ("summary", "core_features", "mvp_scope", "done_criteria",
                                        "tech_recommendation", "chosen_direction", "answers")}),
                         ("디자인", design_brief),
                         ("선택된 개선안", inp.get("proposal")),
                         ("이번 하위 작업", inp.get("subtask")),
                         ("남은 하위 작업(참고)", inp.get("remaining")),
                         ("검증 반려 사유", inp.get("findings")),
                         ("작업 사본 파일 목록", f"총 {total}개\n" + "\n".join(files)),
                         ("허용된 검증 명령", s["check_command_allowlist"]),
                         ("현재 등록된 검증 명령", self.psettings(proj).get("checks")),
                         ("문의 위젯 연결", self._widget_snippet(proj))]
        elif kind == "qa.verify":
            cwd, tools = ws, cfg["permissions"]["model_tools"]
            sections += [("완료 기준", plan.get("done_criteria")),
                         ("선택된 개선안", inp.get("proposal")),
                         ("디자인 요약", {k: design.get(k) for k in ("screens", "user_flows",
                                                                    "accessibility")}),
                         ("제어 프로그램이 실행한 검증 명령 결과", inp.get("check_results")),
                         ("개발 메모", inp.get("dev_notes")),
                         ("변경 diff", gitops.diff_since(ws, cyc["base_commit"]))]
        elif kind == "qa.usability_check":
            cwd, tools = ws, cfg["permissions"]["model_tools"]
            sections += [("디자인 요약", self._latest_design_system(proj["id"])),
                         ("점검 범위", "주요 화면의 사용성·접근성(레이블, 대비, 포커스, 오류 안내). 코드만 읽고 수정하지 않는다.")]
        elif kind == "release.prepare":
            sections += [("품질 검증을 통과한 기능(이것만 홍보)", inp.get("verified_features")),
                         ("미검증 항목", inp.get("untested")),
                         ("기획 요약", {k: plan.get(k) for k in ("summary", "target_customers",
                                                               "mvp_scope")}),
                         ("선택된 개선안", inp.get("proposal")),
                         ("버전", inp.get("version")),
                         ("배포 설정", self.psettings(proj).get("deploy_command") or "아직 없음")]
        elif kind == "release.docs":
            sections += [("검증된 기능", inp.get("verified_features")),
                         ("기존 사용 안내", inp.get("existing_guide"))]
        prompt = departments.build_prompt(task, sections, task.get("last_error"))
        return {
            "task_id": task["id"], "kind": kind, "input": {**inp, "_project": proj["slug"],
                                                           "_plan": plan, "_design": design},
            "system_prompt": departments.role(dept), "prompt": prompt,
            "schema": contracts.for_cli(contracts.SCHEMAS[kind]),
            "model": s["models"].get(dept, "sonnet"), "tools": tools, "cwd": cwd,
            "timeout": task.get("time_limit_sec") or 900,
            "max_budget_usd": s["budget"].get("per_call_usd"),
        }

    def _inbox_docs(self, proj, limit=30000):
        folder = P.inbox_dir(proj["slug"])
        out, used = [], 0
        if not folder.exists():
            return None
        for f in sorted(folder.glob("*")):
            if f.is_file() and f.suffix.lower() in (".md", ".txt", ".csv") and used < limit:
                text = f.read_text(encoding="utf-8", errors="replace")[: limit - used]
                used += len(text)
                out.append(departments.data_block(f.name, text))
        return "\n".join(out) or None

    def _latest_design_system(self, pid):
        r = self.db.one("SELECT design_json FROM cycles WHERE project_id=? AND design_json IS NOT "
                        "NULL ORDER BY id DESC LIMIT 1", (pid,))
        d = jl(r["design_json"], {}) if r else {}
        return d.get("design_system")

    def _widget_snippet(self, proj):
        s = settings.load()
        base = f"http://127.0.0.1:{s['ports']['public']}"
        return (f'<script src="{base}/widget.js" data-project="{proj["slug"]}" '
                f'data-version="{proj["app_version"]}" defer></script>\n'
                "웹 앱은 이 한 줄로 '문의하기' 버튼이 생긴다. 출시 시 주소는 운영 주소로 바뀐다. "
                "웹이 아닌 앱은 같은 주소의 /support 화면을 앱 안 웹뷰로 연다.")

    # ───────────── 결과 적용 ─────────────
    def apply(self, task, out):
        kind = task["kind"]
        handler = getattr(self, "_apply_" + kind.replace(".", "_"))
        summary = handler(task, out)
        self.add_user_actions(task, out.get("user_actions"))
        return summary

    def _apply_planning_define_mvp(self, task, out):
        md = _plan_md(out)
        self.save_artifact(task, f"planning/task-{task['id']}/plan.md", md, "최소 제품 범위·완료 기준")
        self.save_artifact(task, f"planning/task-{task['id']}/plan.json", jd(out), "기획 원본(JSON)")
        self.db.update("cycles", task["cycle_id"], plan_json=jd(out))
        self.add_decision(task["project_id"], "mvp_direction", "제품 방향과 최소 범위 선택",
                          {"summary": out["summary"], "directions": out["directions"],
                           "questions": out["questions"], "mvp_scope": out["mvp_scope"],
                           "done_criteria": out["done_criteria"]},
                          _key("mvp", task["id"]), cycle_id=task["cycle_id"],
                          blocks="디자인·개발 시작")
        self.db.update("projects", task["project_id"], stage="기획 선택 대기")
        return f"방향 {len(out['directions'])}개·질문 {len(out['questions'])}개 정리"

    def _apply_planning_revise(self, task, out):
        cyc = self.cycle(task["cycle_id"])
        plan = jl(cyc["plan_json"], {})
        if out["updated_done_criteria"]:
            plan["done_criteria"] = out["updated_done_criteria"]
        plan.setdefault("clarifications", []).extend(out["clarifications"])
        self.db.update("cycles", cyc["id"], plan_json=jd(plan))
        self._queue_dev(task, "기획 보완 반영", findings=out["clarifications"])
        return out["summary"]

    def _apply_design(self, task, out):
        slug = self.project(task["project_id"])["slug"]
        base = f"design/task-{task['id']}"
        self.save_artifact(task, f"{base}/preview.html", _sanitize_html(out["preview_html"]),
                           "디자인 미리보기", kind="preview")
        for opt in out["direction_options"]:
            oid = re.sub(r"[^A-Za-z0-9_-]", "", opt["id"])[:20] or "opt"
            self.save_artifact(task, f"{base}/option-{oid}.html",
                               _sanitize_html(opt["preview_html"]), f"시안: {opt['title']}",
                               kind="preview")
        slim = {k: v for k, v in out.items() if k not in ("preview_html", "direction_options")}
        slim["preview"] = f"{slug}/{base}/preview.html"
        self.save_artifact(task, f"{base}/design.json", jd(slim), "디자인 명세(JSON)")
        self.db.update("cycles", task["cycle_id"], design_json=jd(slim))
        cyc = self.cycle(task["cycle_id"])
        big = cyc["kind"] == "initial"
        if cyc["proposal_id"]:
            p = self.db.one("SELECT size FROM proposals WHERE id=?", (cyc["proposal_id"],))
            big = big or (p and p["size"] == "large")
        if out["needs_direction_choice"] and len(out["direction_options"]) == 2 and big \
                and task["kind"] != "design.revise":
            self.add_decision(task["project_id"], "design_direction", "디자인 방향 시안 선택",
                              {"summary": out["summary"],
                               "options": [{"id": o["id"], "title": o["title"],
                                            "description": o["description"],
                                            "preview": f"{slug}/{base}/option-"
                                                       f"{re.sub(r'[^A-Za-z0-9_-]', '', o['id'])[:20] or 'opt'}.html"}
                                           for o in out["direction_options"]]},
                              _key("dd", task["id"]), cycle_id=cyc["id"], blocks="개발 시작")
            return "디자인 시안 2개 준비, 방향 선택 대기"
        self._queue_dev(task, "디자인에 따라 구현")
        return out["summary"][:200]

    _apply_design_initial = _apply_design
    _apply_design_improvement = _apply_design
    _apply_design_revise = _apply_design

    def _queue_dev(self, task, subtask, findings=None, remaining=None, chain_inc=True):
        cyc = self.cycle(task["cycle_id"])
        proj = self.project(task["project_id"])
        ws = P.workspace(proj["slug"])
        gitops.ensure_repo(ws)
        if not cyc["base_commit"]:
            self.db.update("cycles", cyc["id"], base_commit=gitops.head(ws))
        if chain_inc:
            self.db.update("cycles", cyc["id"], dev_chain=cyc["dev_chain"] + 1)
        self.handoff(task, "development", subtask)
        return self.create_task(task["project_id"], "development.implement", f"개발: {subtask}"[:80],
                                {"subtask": subtask, "findings": findings, "remaining": remaining,
                                 "proposal": self._proposal_brief(cyc)},
                                cycle_id=cyc["id"], code_version=gitops.head(ws),
                                done_criteria="변경이 적용되고 Git 복구 지점이 만들어짐")

    def _proposal_brief(self, cyc):
        if not cyc or not cyc["proposal_id"]:
            return None
        p = self.db.one("SELECT title, body_json, scope_note FROM proposals WHERE id=?",
                        (cyc["proposal_id"],))
        return {"title": p["title"], "scope_note": p["scope_note"], **jl(p["body_json"], {})}

    def _apply_development_implement(self, task, out):
        cyc = self.cycle(task["cycle_id"])
        if not cyc or not cyc["approved"]:
            raise GateError("승인되지 않은 작업의 변경은 적용하지 않습니다")
        proj = self.project(task["project_id"])
        ws = P.workspace(proj["slug"])
        gitops.ensure_repo(ws)
        user_dirty = set(gitops.dirty_files(ws))
        planned = []
        for ch in out["changes"]:
            try:
                target = safe_join(ws, ch["path"])
                assert_inside(target, [P.projects])
            except SafetyError as e:
                raise ApplyError(f"경로 거부: {e}")
            rel = str(target.relative_to(ws.resolve())).replace("\\", "/")
            if rel in user_dirty:
                raise ApplyError(f"사용자가 수정 중인 파일과 겹칩니다(보존): {rel}")
            if ch["op"] == "edit":
                if not target.exists():
                    raise ApplyError(f"수정할 파일이 없습니다: {rel}")
                text = target.read_text(encoding="utf-8")
                for e in ch["edits"]:
                    if text.count(e["old"]) != 1:
                        raise ApplyError(f"{rel}: 바꿀 문자열이 정확히 한 번 있어야 합니다")
                    text = text.replace(e["old"], e["new"], 1)
                planned.append((rel, text))
            else:
                if ch["op"] == "create" and target.exists():
                    raise ApplyError(f"이미 있는 파일입니다(replace 사용): {rel}")
                planned.append((rel, ch["content"]))
        journal = P.journal / f"task-{task['id']}.json"
        journal.write_text(jd({"paths": [r for r, _ in planned], "ts": now()}), encoding="utf-8")
        for rel, text in planned:
            guarded_write(ws, rel, text, [P.projects])
        commit = gitops.commit_paths(ws, [r for r, _ in planned],
                                     f"AI-Team task {task['id']}: {out['summary'][:60]}")
        journal.unlink(missing_ok=True)
        allow = settings.load()["check_command_allowlist"]
        ps = self.psettings(proj)
        cur = list(ps.get("checks") or [])
        for c in out["proposed_checks"]:
            c = " ".join(c.split())
            if any(c == a or c.startswith(a + " ") for a in allow) and c not in cur:
                cur.append(c)
        self.set_psettings(proj, {"checks": cur[:5]})
        self.save_artifact(task, f"development/task-{task['id']}.json",
                           jd({k: v for k, v in out.items() if k != "changes"} |
                              {"files": [r for r, _ in planned], "commit": commit}),
                           "개발 결과 기록")
        cyc = self.cycle(cyc["id"])
        if not out["work_complete"] and out["remaining_subtasks"] and cyc["dev_chain"] < MAX_DEV_CHAIN:
            nxt, rest = out["remaining_subtasks"][0], out["remaining_subtasks"][1:]
            self.db.update("cycles", cyc["id"], dev_chain=cyc["dev_chain"] + 1)
            self.create_task(proj["id"], "development.implement", f"개발: {nxt}"[:80],
                             {"subtask": nxt, "remaining": rest,
                              "proposal": self._proposal_brief(cyc)},
                             cycle_id=cyc["id"], code_version=commit)
            return f"{len(planned)}개 파일 변경, 다음 하위 작업 진행"
        self.handoff(task, "qa", "구현 완료, 검증 요청")
        self.create_task(proj["id"], "qa.verify", "품질 검증", {
            "dev_notes": out["notes_for_qa"], "connections": out["connections"],
            "proposal": self._proposal_brief(cyc)}, cycle_id=cyc["id"], code_version=commit,
            done_criteria="완료 기준·디자인·실제 앱 대조, 검증 명령 실행 결과로 판정")
        return f"{len(planned)}개 파일 변경, 커밋 {commit[:8]}"

    # QA: 검증 명령은 코드가 먼저 실행한다. 실패가 있으면 모델을 부르지 않는다.
    def qa_precheck(self, task):
        proj = self.project(task["project_id"])
        cyc = self.cycle(task["cycle_id"])
        ws = P.workspace(proj["slug"])
        cmds = self.psettings(proj).get("checks") or []
        results = checks.run_checks(ws, cmds, task["id"]) if cmds else []
        findings = []
        for r in results:
            if not r["ok"]:
                findings.append({"severity": "high", "area": "development",
                                 "description": f"검증 명령 실패: {r['command']}",
                                 "evidence": r["output"][-600:]})
        findings += self._test_shrink(ws, cyc["base_commit"])
        findings += self._trivial_checks(ws, cmds)
        if not cmds:
            findings.append({"severity": "high", "area": "development",
                             "description": "실행할 자동 검증 명령이 없습니다. 의미 있는 테스트와 "
                                            "허용 목록의 검증 명령(proposed_checks)을 추가해야 합니다",
                             "evidence": "검증 증거가 없으면 출시로 넘기지 않습니다"})
        inp = jl(task["input_json"], {})
        inp["check_results"] = [{k: r[k] for k in ("command", "ok", "exit", "seconds")} |
                                {"output_tail": r["output"][-1500:]} for r in results]
        self.db.update("tasks", task["id"], input_json=jd(inp))
        return results, findings

    def _trivial_checks(self, ws, cmds):
        """아무것도 검사하지 않는 검증 스크립트(echo 등)는 검증 증거로 인정하지 않는다."""
        out = []
        pkg = ws / "package.json"
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}) if pkg.exists() else {}
        except ValueError:
            scripts = {}
        for c in cmds:
            m = re.fullmatch(r"npm (?:run (\S+)|(test))(?: .*)?", c)
            if not m:
                continue
            name = m.group(1) or m.group(2)
            body = scripts.get(name)
            if body is None:
                out.append({"severity": "high", "area": "development",
                            "description": f"검증 명령의 스크립트가 없습니다: {c}", "evidence": "package.json"})
            elif re.fullmatch(r"\s*(echo\b.*|exit 0|true|:|)\s*", body, re.I):
                out.append({"severity": "high", "area": "development",
                            "description": f"아무것도 검사하지 않는 검증 스크립트입니다: {name}",
                            "evidence": f"package.json scripts.{name} = {body[:120]}"})
        return out

    def _test_shrink(self, ws, base):
        out = []
        for path in gitops.files_changed_since(ws, base):
            if not re.search(r"(^|/)(tests?|__tests__|spec)(/|$)|(test|spec)[^/]*\.\w+$", path, re.I):
                continue
            before = gitops.file_at(ws, base, path)
            after = gitops.file_at(ws, "HEAD", path)
            if before and (after is None or len(after.splitlines()) < len(before.splitlines()) * 0.5):
                out.append({"severity": "critical", "area": "development",
                            "description": f"테스트 축소·삭제 감지: {path}",
                            "evidence": f"{len(before.splitlines())}줄 → "
                                        f"{len((after or '').splitlines())}줄"})
        return out

    def finish_qa(self, task, verdict, summary, findings, verified, untested, results):
        cyc = self.cycle(task["cycle_id"])
        if any(f["severity"] in ("critical", "high") for f in findings):
            verdict = "fail"  # 기준을 낮추지 않는다
        evidence = "검증 명령 " + (", ".join(f"{r['command']}={'통과' if r['ok'] else '실패'}"
                                         for r in results) or "없음")
        self.db.insert("qa_results", project_id=task["project_id"], cycle_id=cyc["id"],
                       task_id=task["id"], verdict=verdict, summary=summary,
                       checks_json=jd([{k: r[k] for k in ("command", "ok", "exit", "seconds")}
                                       for r in results]),
                       findings_json=jd(findings), evidence=evidence, created_at=now())
        self.save_artifact(task, f"qa/task-{task['id']}.json",
                           jd({"verdict": verdict, "summary": summary, "findings": findings,
                               "verified_features": verified, "untested": untested,
                               "checks": results}), f"검증 결과: {verdict}")
        if verdict == "pass":
            self.db.update("cycles", cyc["id"], verified_json=jd(
                {"verified_features": verified, "untested": untested}))
            proj = self.project(task["project_id"])
            version = proj["app_version"] if cyc["kind"] == "initial" else _bump(proj["app_version"])
            self.handoff(task, "release", "검증 통과, 출시 준비 요청")
            self.create_task(task["project_id"], "release.prepare", f"출시 준비 v{version}",
                             {"verified_features": verified, "untested": untested,
                              "version": version, "proposal": self._proposal_brief(cyc)},
                             cycle_id=cyc["id"],
                             done_criteria="검증된 기능만 담은 소개·안내·배포·복구 방법")
            return f"통과 ({evidence})"
        sig = _key(sorted((f["area"], f["description"][:60]) for f in findings))
        sigs = jl(cyc["fail_sigs"], [])
        sigs.append(sig)
        self.db.update("cycles", cyc["id"], fail_sigs=jd(sigs))
        if sigs.count(sig) > 2 or len(sigs) > MAX_QA_FAILS:
            self.db.update("cycles", cyc["id"], status="blocked")
            self.add_decision(task["project_id"], "qa_escalation",
                              "같은 원인으로 검증이 반복 실패했습니다", {
                                  "findings": findings, "evidence": evidence,
                                  "options": ["다시 시도", "이 작업 보류"]},
                              _key("qa-esc", cyc["id"], len(sigs)), cycle_id=cyc["id"],
                              blocks="이 작업의 출시")
            return f"실패 반복 → 사용자 보고 ({evidence})"
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        top = sorted(findings, key=lambda f: order[f["severity"]])[0] if findings else \
            {"area": "development"}
        area = top["area"]
        if area == "planning":
            self.handoff(task, "planning", "검증 실패: 기획 보완 필요")
            self.create_task(task["project_id"], "planning.revise", "검증 반려: 기획 보완",
                             {"findings": findings}, cycle_id=cyc["id"],
                             code_version=str(len(sigs)))
        elif area == "design":
            self.handoff(task, "design", "검증 실패: 디자인 수정 필요")
            self.create_task(task["project_id"], "design.revise", "검증 반려: 디자인 수정",
                             {"findings": findings}, cycle_id=cyc["id"],
                             code_version=str(len(sigs)))
        else:
            self._queue_dev(task, "검증 반려 수정", findings=findings)
        return f"실패 → {departments.NAMES[area]}로 반려 ({evidence})"

    def _apply_qa_verify(self, task, out):
        inp = jl(task["input_json"], {})
        results = inp.get("check_results") or []
        return self.finish_qa(task, out["verdict"], out["summary"], out["findings"],
                              out["verified_features"], out["untested"], results)

    def _apply_qa_usability_check(self, task, out):
        self.save_artifact(task, f"qa/usability-{task['id']}.json", jd(out), "사용성·접근성 점검")
        return f"점검 결과 {len(out['findings'])}건 기록(개발은 사용자 선택 후)"

    def _apply_release_prepare(self, task, out):
        cyc = self.cycle(task["cycle_id"])
        inp = jl(task["input_json"], {})
        verified = [v.lower() for v in inp.get("verified_features") or []]
        kept, dropped = [], []
        for f in out["store_listing"]["features"]:
            fl = f.lower()
            (kept if any(v in fl or fl in v for v in verified) else dropped).append(f)
        out["store_listing"]["features"] = kept
        sl = out["store_listing"]
        base = f"release/v{inp['version']}"
        self.save_artifact(task, f"{base}/store_listing.md",
                           f"# {sl['name']}\n\n{sl['tagline']}\n\n{sl['description']}\n\n## 기능(검증됨)\n"
                           + "\n".join(f"- {f}" for f in kept)
                           + ("\n\n## 검증되지 않아 제외한 문구\n" + "\n".join(f"- {d}" for d in dropped)
                              if dropped else ""), "판매 소개")
        self.save_artifact(task, f"{base}/user_guide.md", out["user_guide_md"], "사용 안내")
        self.save_artifact(task, f"{base}/release_notes.md", out["release_notes"], "변경 사항")
        self.save_artifact(task, f"{base}/deploy_and_rollback.md",
                           "# 배포 순서\n" + "\n".join(f"{i+1}. {s}" for i, s in
                                                     enumerate(out["deploy_plan"])) +
                           "\n\n# 복구(이전 버전으로 되돌리기)\n" +
                           "\n".join(f"{i+1}. {s}" for i, s in enumerate(out["rollback_plan"])),
                           "배포·복구 방법")
        rid = self.db.insert("releases", project_id=task["project_id"], cycle_id=cyc["id"],
                             version=inp["version"], status="prepared",
                             notes=out["release_notes"][:2000], created_at=now())
        self.add_decision(task["project_id"], "deploy_approval", f"v{inp['version']} 배포 승인", {
            "release_id": rid, "version": inp["version"], "verified_features": kept,
            "dropped_claims": dropped, "deploy_plan": out["deploy_plan"],
            "rollback_plan": out["rollback_plan"],
            "deploy_command": self.psettings(self.project(task["project_id"])).get("deploy_command")},
            _key("deploy", rid), cycle_id=cyc["id"], blocks="운영 배포")
        self.db.update("projects", task["project_id"], stage="배포 승인 대기")
        return f"출시 자료 준비, 배포 승인 대기 (제외된 문구 {len(dropped)}개)"

    def _apply_release_docs(self, task, out):
        self.save_artifact(task, f"release/docs-{task['id']}/user_guide.md", out["user_guide_md"],
                           "사용 안내(보완)")
        return "사용 안내 보완"

    def _apply_planning_analyze_inquiries(self, task, out):
        pid = task["project_id"]
        inp = jl(task["input_json"], {})
        valid_ids = set(inp["inquiry_ids"])
        refmap = {}
        for c in out["clusters"]:
            m = re.fullmatch(r"C(\d+)", c["ref"])
            fields = dict(title=c["title"][:120], problem=c["problem"],
                          requested_feature=c["requested_feature"],
                          underlying_problem=c["underlying_problem"], kind=c["kind"],
                          severity=c["severity"], impact=c["impact"],
                          reproducible=int(c["reproducible"]), facts_json=jd(c["facts"]),
                          estimates_json=jd(c["estimates"]), quotes_json=jd(c["customer_words"]),
                          reply_draft=c["reply_draft"], updated_at=now())
            if m and self.db.one("SELECT 1 FROM clusters WHERE id=? AND project_id=?",
                                 (int(m.group(1)), pid)):
                cid = int(m.group(1))
                self.db.update("clusters", cid, **fields)
            else:
                cid = self.db.insert("clusters", project_id=pid, created_at=now(), **fields)
            refmap[c["ref"]] = cid
        assigned = 0
        for a in out["assignments"]:
            cid = refmap.get(a["cluster"])
            if cid is None:
                m = re.fullmatch(r"C(\d+)", a["cluster"])
                cid = int(m.group(1)) if m and self.db.one(
                    "SELECT 1 FROM clusters WHERE id=? AND project_id=?", (int(m.group(1)), pid)) else None
            if cid and a["inquiry_id"] in valid_ids:
                self.db.x("UPDATE inquiries SET cluster_id=?, status='analyzed' WHERE id=? AND "
                          "project_id=?", (cid, a["inquiry_id"], pid))
                assigned += 1
        self.db.meta_set(f"last_analysis:{pid}", now())
        return f"문의 {assigned}/{len(valid_ids)}건을 {len(refmap)}개 문제로 묶음"

    def _apply_planning_propose_improvements(self, task, out):
        pid = task["project_id"]
        inp = jl(task["input_json"], {})
        ref_ok = {c["ref"]: c["id"] for c in inp["clusters"]}
        made = []
        for p in out["proposals"][:3]:
            cids = [ref_ok[r] for r in p["evidence_cluster_refs"] if r in ref_ok]
            if not cids:
                continue  # 근거 없는 개선안은 제시하지 않는다
            iids = [r["id"] for r in self.db.all(
                "SELECT id FROM inquiries WHERE cluster_id IN (%s)" % ",".join("?" * len(cids)),
                tuple(cids))]
            prid = self.db.insert("proposals", project_id=pid, batch_task=task["id"],
                                  title=p["title"][:120], body_json=jd(p), size=p["size"],
                                  status="proposed", cluster_ids=jd(cids), inquiry_ids=jd(iids),
                                  created_at=now())
            made.append(prid)
        if made:
            self.add_decision(pid, "proposal_selection", f"개선안 {len(made)}개 중 선택",
                              {"proposal_ids": made}, _key("props", task["id"]),
                              blocks="개선 디자인·개발")
        return f"개선안 {len(made)}개 제시"

    def _apply_planning_strengthen_proposal(self, task, out):
        inp = jl(task["input_json"], {})
        prop = self.db.one("SELECT * FROM proposals WHERE id=?", (inp["proposal_id"],))
        body = jl(prop["body_json"], {})
        body["strengthened"] = out
        self.db.update("proposals", prop["id"], body_json=jd(body))
        return "개선안 근거 보강"

    # ───────────── 사용자 결정 처리 ─────────────
    def answer_decision(self, did, answer):
        d = self.db.one("SELECT * FROM decisions WHERE id=?", (did,))
        if not d or d["status"] != "pending":
            raise ValueError("이미 처리되었거나 없는 결정입니다")
        body = jl(d["body_json"], {})
        kind = d["kind"]
        pid = d["project_id"]
        cyc = self.cycle(d["cycle_id"])
        fake = {"id": None, "project_id": pid, "dept": DECISION_DEPT.get(kind) or "planning",
                "cycle_id": d["cycle_id"]}
        if kind == "mvp_direction":
            ids = [x["id"] for x in body["directions"]]
            if answer.get("direction_id") not in ids:
                raise ValueError("방향을 하나 고르세요")
            plan = jl(cyc["plan_json"], {})
            plan["chosen_direction"] = next(x for x in body["directions"]
                                            if x["id"] == answer["direction_id"])
            plan["answers"] = answer.get("answers") or {}
            plan["user_note"] = (answer.get("note") or "")[:2000]
            self.db.update("cycles", cyc["id"], plan_json=jd(plan), approved=1)
            self._close_decision(d, answer)
            self.db.update("projects", pid, stage="디자인 중")
            self.handoff(fake, "design", "선택된 기획 전달")
            self.create_task(pid, "design.initial", "사용자 흐름·화면·디자인 체계 설계",
                             {"direction": answer["direction_id"]}, cycle_id=cyc["id"],
                             done_criteria="흐름·화면·상태·디자인 체계·미리보기 완성")
        elif kind == "design_direction":
            opts = {o["id"]: o for o in body["options"]}
            if answer.get("option_id") not in opts:
                raise ValueError("시안을 하나 고르세요")
            design = jl(cyc["design_json"], {})
            design["chosen_direction"] = opts[answer["option_id"]]
            self.db.update("cycles", cyc["id"], design_json=jd(design))
            self._close_decision(d, answer)
            self._queue_dev(fake | {"dept": "design"}, "선택한 디자인 시안으로 구현")
        elif kind == "deploy_approval":
            if answer.get("approve"):
                self._close_decision(d, answer)
                return self.deploy(pid, body["release_id"])
            self._close_decision(d, answer)
            self.db.update("releases", body["release_id"], status="held")
            self.db.event("release", "배포 보류(사용자 결정)", project_id=pid, dept="release")
        elif kind == "qa_escalation":
            self._close_decision(d, answer)
            if answer.get("choice") == "다시 시도":
                self.db.update("cycles", cyc["id"], status="open", fail_sigs="[]")
                self._queue_dev(fake | {"dept": "qa"}, "검증 반복 실패 재시도",
                                findings=body.get("findings"))
            else:
                self.db.update("cycles", cyc["id"], status="held")
        elif kind == "user_action":
            self._close_decision(d, answer, status="done")
            if self.db.meta_get("auth_problem") and "로그인" in d["title"]:
                self.db.meta_set("auth_problem", None)
            self._maybe_mark_deployed(d)
        elif kind == "proposal_selection":
            raise ValueError("개선안은 각 안의 선택/보류/거절 버튼으로 결정합니다")
        return None

    def _close_decision(self, d, answer, status="answered"):
        self.db.update("decisions", d["id"], status=status, answer_json=jd(answer),
                       answered_at=now())
        self.db.event("decision", f"사용자 결정: {d['title']}", project_id=d["project_id"],
                      dept=DECISION_DEPT.get(d["kind"]))

    def decide_proposal(self, prid, choice, scope=""):
        p = self.db.one("SELECT * FROM proposals WHERE id=?", (prid,))
        if not p or p["status"] not in ("proposed", "held"):
            raise ValueError("결정할 수 없는 개선안입니다")
        if choice not in ("select", "hold", "reject"):
            raise ValueError("선택/보류/거절 중 하나")
        status = {"select": "selected", "hold": "held", "reject": "rejected"}[choice]
        self.db.update("proposals", prid, status=status, scope_note=scope[:1000], decided_at=now())
        self.db.event("decision", f"개선안 {('선택', '보류', '거절')[['select', 'hold', 'reject'].index(choice)]}: "
                                  f"{p['title']}", project_id=p["project_id"], dept="planning")
        if choice == "select":
            cid = self.db.insert("cycles", project_id=p["project_id"], kind="improvement",
                                 proposal_id=prid, approved=1, created_at=now(),
                                 plan_json=self._last_plan(p["project_id"]))
            self.db.update("proposals", prid, status="in_progress")
            for c in jl(p["cluster_ids"], []):
                self.db.x("UPDATE clusters SET status='in_progress' WHERE id=?", (c,))
                self.db.x("UPDATE inquiries SET status='in_progress' WHERE cluster_id=?", (c,))
            fake = {"id": None, "project_id": p["project_id"], "dept": "planning", "cycle_id": cid}
            self.handoff(fake, "design", f"선택된 개선안: {p['title']}")
            self.create_task(p["project_id"], "design.improvement", f"개선 디자인: {p['title']}"[:80],
                             {"proposal": {"title": p["title"], "scope_note": scope,
                                           **jl(p["body_json"], {})}}, cycle_id=cid,
                             done_criteria="선택된 범위의 화면·흐름 변경 설계")
        # 배치의 모든 안이 결정되면 결정 요청 종료
        batch = self.db.all("SELECT status FROM proposals WHERE batch_task=?", (p["batch_task"],))
        if all(b["status"] not in ("proposed",) for b in batch):
            for d in self.db.all("SELECT * FROM decisions WHERE kind='proposal_selection' AND "
                                 "status='pending' AND project_id=?", (p["project_id"],)):
                if prid in jl(d["body_json"], {}).get("proposal_ids", []):
                    self._close_decision(d, {"proposals": [dict(b) for b in batch]})

    def _last_plan(self, pid):
        r = self.db.one("SELECT plan_json FROM cycles WHERE project_id=? AND plan_json IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1", (pid,))
        return r["plan_json"] if r else None

    # ───────────── 배포 ─────────────
    def deploy(self, pid, release_id):
        proj = self.project(pid)
        rel = self.db.one("SELECT * FROM releases WHERE id=?", (release_id,))
        cmd = self.psettings(proj).get("deploy_command")
        self.db.update("releases", release_id, status="approved")
        if cmd:
            from .safety import SafetyError as SE, validate_check_command
            try:
                validate_check_command(cmd, [cmd.split()[0]])
                res = checks.run_checks(P.workspace(proj["slug"]), [cmd], f"deploy-{release_id}")
                if res and res[0]["ok"]:
                    return self.mark_deployed(pid, release_id, "배포 명령 성공")
                self.db.event("error", "배포 명령 실패: " + (res[0]["output"][-300:] if res else ""),
                              project_id=pid, dept="release")
            except SE as e:
                self.db.event("error", f"배포 명령 거부: {e}", project_id=pid, dept="release")
        self.add_decision(pid, "user_action", f"v{rel['version']} 배포 실행", {
            "todo": f"v{rel['version']} 배포 실행", "why_now": "배포를 승인했습니다",
            "where": "artifacts의 deploy_and_rollback.md", "steps": ["배포·복구 방법 문서를 따라 배포",
                                                                   "끝나면 '완료'를 누르기"],
            "cost": {"value": "배포 서비스에 따라 다름", "kind": "unknown"},
            "report_back": "배포한 주소(비밀정보 제외)", "blocked_work": "배포 완료 기록",
            "meanwhile": "다른 실행 가능한 작업", "release_id": release_id},
            _key("deploy-ua", release_id), blocks="배포 완료 기록")
        return None

    def _maybe_mark_deployed(self, d):
        body = jl(d["body_json"], {})
        if body.get("release_id"):
            self.mark_deployed(d["project_id"], body["release_id"], "사용자가 배포 완료 확인")

    def mark_deployed(self, pid, release_id, how):
        rel = self.db.one("SELECT * FROM releases WHERE id=?", (release_id,))
        if rel["status"] == "deployed":
            return
        self.db.update("releases", release_id, status="deployed", deployed_at=now())
        self.db.update("projects", pid, stage="운영 중", app_version=rel["version"])
        cyc = self.cycle(rel["cycle_id"])
        self.db.update("cycles", cyc["id"], status="deployed", closed_at=now())
        if cyc["proposal_id"]:
            p = self.db.one("SELECT * FROM proposals WHERE id=?", (cyc["proposal_id"],))
            self.db.update("proposals", p["id"], status="deployed")
            for c in jl(p["cluster_ids"], []):
                self.db.x("UPDATE clusters SET status='deployed' WHERE id=?", (c,))
                self.db.x("UPDATE inquiries SET status='deployed' WHERE cluster_id=?", (c,))
        self.db.event("release", f"v{rel['version']} 배포 완료 ({how}). 고객 문제 해결 확인은 별도",
                      project_id=pid, dept="release", is_mock=self._is_mock(pid))

    def confirm_resolved(self, cluster_id):
        c = self.db.one("SELECT * FROM clusters WHERE id=?", (cluster_id,))
        if not c or c["status"] != "deployed":
            raise ValueError("배포 완료된 문제만 해결 확인할 수 있습니다")
        self.db.update("clusters", cluster_id, status="resolved_confirmed", updated_at=now())
        self.db.x("UPDATE inquiries SET status='resolved_confirmed' WHERE cluster_id=?",
                  (cluster_id,))
        self.db.event("release", f"고객 문제 해결 확인: {c['title']}", project_id=c["project_id"],
                      dept="release")

    # ───────────── 자동 판단(모델 호출 없음) ─────────────
    def housekeeping(self, pid):
        proj = self.project(pid)
        inquiries.import_inbox_files(self.db, proj)
        if proj["stage"] != "운영 중":
            return
        s = settings.load()["analysis"]
        new = self.db.all("SELECT id, created_at FROM inquiries WHERE project_id=? AND "
                          "cluster_id IS NULL AND status='received' ORDER BY id", (pid,))
        if new:
            oldest = datetime.fromisoformat(new[0]["created_at"])
            age_h = (datetime.now(timezone.utc) - oldest).total_seconds() / 3600
            if len(new) >= s["min_new_inquiries"] or age_h >= s["max_wait_hours"]:
                self._queue_analysis(pid, [r["id"] for r in new])
        self._maybe_propose(pid)

    def _queue_analysis(self, pid, ids, fallback=False):
        return self.create_task(pid, "planning.analyze_inquiries", f"새 문의 {len(ids)}건 분석",
                                {"inquiry_ids": ids[:60]}, is_fallback=fallback,
                                done_criteria="모든 문의가 문제 묶음에 연결되고 사실·추정 구분")

    def _maybe_propose(self, pid):
        if self.db.one("SELECT 1 FROM decisions WHERE project_id=? AND kind='proposal_selection' "
                       "AND status='pending'", (pid,)):
            return
        if self.db.one("SELECT 1 FROM tasks WHERE project_id=? AND status IN ('queued','running') "
                       "AND kind IN ('planning.analyze_inquiries','planning.propose_improvements')",
                       (pid,)):
            return
        covered = set()
        for p in self.db.all("SELECT cluster_ids FROM proposals WHERE project_id=? AND status IN "
                             "('proposed','held','selected','in_progress')", (pid,)):
            covered |= set(jl(p["cluster_ids"], []))
        open_c = [c for c in self.db.all("SELECT * FROM clusters WHERE project_id=? AND "
                                         "status='open'", (pid,)) if c["id"] not in covered]
        if not open_c:
            return
        data = []
        for c in open_c:
            st = inquiries.cluster_stats(self.db, c["id"])
            data.append({"ref": f"C{c['id']}", "id": c["id"], "title": c["title"],
                         "problem": c["problem"], "kind": c["kind"], "severity": c["severity"],
                         "requested_feature": c["requested_feature"],
                         "underlying_problem": c["underlying_problem"],
                         "facts": jl(c["facts_json"], []), "estimates": jl(c["estimates_json"], []),
                         "문의건수": st["count"], "고객수": st["customers"],
                         "최근7일": st["last7"], "이전7일": st["prev7"], "추세": st["trend"]})
        existing = [p["title"] for p in self.db.all("SELECT title FROM proposals WHERE project_id=?",
                                                     (pid,))]
        self.create_task(pid, "planning.propose_improvements", "개선안 최대 3개 제시",
                         {"clusters": data, "existing": existing},
                         done_criteria="근거 문의가 연결된 개선안 최대 3개")

    def fallback_work(self, pid):
        """사용자 결정이 오래 대기 중이고 실행할 일이 없을 때 우선순위대로 다른 업무 하나를 고른다."""
        proj = self.project(pid)
        s = settings.load()
        wait_h = s["decision_wait_hours"]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=wait_h)).isoformat()
        if not self.db.one("SELECT 1 FROM decisions WHERE project_id=? AND status='pending' "
                           "AND requested_at<=?", (pid, cutoff)):
            return None
        # 2) 사전 승인된 유지보수 범위의 재현 가능한 버그
        if s["maintenance"].get("auto_bugfix") and proj["stage"] == "운영 중":
            c = self.db.one("SELECT * FROM clusters WHERE project_id=? AND status='open' AND "
                            "kind='bug' AND reproducible=1 AND severity IN ('critical','high','medium') "
                            "ORDER BY id LIMIT 1", (pid,))
            if c:
                cid = self.db.insert("cycles", project_id=pid, kind="bugfix", approved=1,
                                     created_at=now(), plan_json=self._last_plan(pid))
                self.db.x("UPDATE clusters SET status='in_progress' WHERE id=?", (c["id"],))
                fake = {"id": None, "project_id": pid, "dept": "planning", "cycle_id": cid}
                return self._queue_dev(fake, f"유지보수 버그 수정: {c['title']}"[:70],
                                       findings=[{"problem": c["problem"],
                                                  "facts": jl(c["facts_json"], [])}])
        # 3) 새 문의 정리
        if proj["stage"] == "운영 중":
            new = [r["id"] for r in self.db.all(
                "SELECT id FROM inquiries WHERE project_id=? AND cluster_id IS NULL AND "
                "status='received'", (pid,))]
            if new:
                return self._queue_analysis(pid, new, fallback=True)
        # 4) 기존 개선안 근거 보강(안마다 한 번)
        for p in self.db.all("SELECT * FROM proposals WHERE project_id=? AND status IN "
                             "('proposed','held') ORDER BY id", (pid,)):
            if "strengthened" in jl(p["body_json"], {}):
                continue
            iq = self.db.all("SELECT id,category,content,expected,screen FROM inquiries WHERE id IN "
                             "(%s)" % ",".join("?" * len(jl(p["inquiry_ids"], []))),
                             tuple(jl(p["inquiry_ids"], []))) if jl(p["inquiry_ids"], []) else []
            return self.create_task(pid, "planning.strengthen_proposal", f"근거 보강: {p['title']}"[:80],
                                    {"proposal_id": p["id"], "proposal": jl(p["body_json"], {}),
                                     "inquiries": iq[:40]}, is_fallback=True,
                                    done_criteria="근거 추가와 남은 질문 정리(새 개선안 없음)")
        ws = P.workspace(proj["slug"])
        if (ws / ".git").exists():
            head = gitops.head(ws)
            # 5) 사용성·접근성 점검(코드 버전마다 한 번)
            if self.db.one("SELECT 1 FROM cycles WHERE project_id=? AND verified_json IS NOT NULL",
                           (pid,)):
                tid = self._once(pid, "qa.usability_check", "사용성·접근성 점검", {}, head)
                if tid:
                    return tid
            # 6) 사용 안내
            v = self.db.one("SELECT verified_json FROM cycles WHERE project_id=? AND verified_json "
                            "IS NOT NULL ORDER BY id DESC LIMIT 1", (pid,))
            if v and self.db.one("SELECT 1 FROM releases WHERE project_id=?", (pid,)):
                tid = self._once(pid, "release.docs", "사용 안내 보완",
                                 {"verified_features": jl(v["verified_json"], {}).get(
                                     "verified_features")}, head)
                if tid:
                    return tid
        return None

    def _once(self, pid, kind, title, inp, version):
        key = _key(pid, kind, inp, version, None)
        if self.db.one("SELECT 1 FROM tasks WHERE dedup_key=?", (key,)):
            return None
        return self.create_task(pid, kind, title, inp, is_fallback=True, code_version=version,
                                done_criteria="결과 1건 기록, 같은 버전에서 반복하지 않음")


# ───────────── 도우미 ─────────────
def _bump(v):
    try:
        a, b, c = (int(x) for x in v.split("."))
        return f"{a}.{b}.{c + 1}"
    except ValueError:
        return v + ".1"


def _sanitize_html(html):
    """미리보기는 스크립트 없이 보여준다(관리 화면에서도 sandbox iframe으로 표시)."""
    html = re.sub(r"<script\b.*?</script\s*>", "", html, flags=re.I | re.S)
    html = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*')", "", html, flags=re.I)
    return html


def _plan_md(o):
    L = [f"# 기획 요약\n\n{o['summary']}\n", "## 목표 고객"]
    L += [f"- **{c['segment']}**: {c['description']}" for c in o["target_customers"]]
    L.append("\n## 문제 (가설/확인 구분)")
    L += [f"- [{'확인' if p['basis'] == 'confirmed' else '가설'}] {p['problem']} — {p['source']}"
          for p in o["problems"]]
    L.append("\n## 핵심 기능")
    L += [f"- ({f['priority']}) **{f['name']}**: {f['description']}" for f in o["core_features"]]
    L.append("\n## 최소 제품 범위\n**포함**")
    L += [f"- {x}" for x in o["mvp_scope"]["included"]]
    L.append("\n**제외**")
    L += [f"- {x}" for x in o["mvp_scope"]["excluded"]]
    L.append("\n## 완료 기준")
    L += [f"- [ ] {x}" for x in o["done_criteria"]]
    t = o["tech_recommendation"]
    L.append(f"\n## 추천 기술 구성\n{t['platform']} / {t['stack']} — {t['reason']}")
    return "\n".join(L)
