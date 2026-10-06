"use strict";
// AI팀 관리 화면. 화면 갱신은 상태 API만 읽으며 모델을 호출하지 않는다.
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const NA = '<span class="muted">미수집</span>';
let S = null, projectId = null, tab = "timeline", lastDecisionKey = "", lastPanelKey = "";
let previewPath = null;

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-AITeam": "1" }, body: JSON.stringify(body) };
  const r = await fetch(path, { credentials: "same-origin", ...opt });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
async function act(path, body, okMsg) {
  try { await api(path, body); if (okMsg) toast(okMsg); lastDecisionKey = ""; lastPanelKey = ""; await refresh(); }
  catch (e) { alert(e.message); }
}
function toast(m) { const b = $("#banners"); const d = document.createElement("div"); d.className = "banner info"; d.textContent = m; b.prepend(d); setTimeout(() => d.remove(), 3500); }

async function login() {
  const m = location.hash.match(/t=([A-Za-z0-9_\-]+)/);
  if (m) { try { await api("/api/login", { token: m[1] }); } catch (e) {} history.replaceState(null, "", location.pathname); }
}

const num = (v) => (v === null || v === undefined ? NA : Number(v).toLocaleString("ko-KR"));
const usd = (v) => (v === null || v === undefined ? NA : "$" + Number(v).toFixed(2));
const time = (ts) => { if (!ts) return ""; const d = new Date(ts); return d.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
const hm = (ts) => { if (!ts) return ""; const d = new Date(ts); return d.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" }); };
const SIZE = { small: "작음", medium: "보통", large: "큼" };
const SEV = { critical: "치명", high: "높음", medium: "보통", low: "낮음" };
const KIND = { bug: "오류", feature: "기능 요청", usability: "사용성", question: "질문", other: "기타" };
const PSTATUS = { proposed: "선택 대기", held: "보류", rejected: "거절", selected: "선택됨", in_progress: "진행 중", deployed: "배포 완료" };
const TSTATUS = { queued: "대기열", running: "실행 중", done: "완료", failed: "실패(보고됨)", cancelled: "취소", needs_verification: "검증 필요" };
const EVK = { project: "프로젝트", queued: "대기열 추가", start: "시작", done: "완료", handoff: "인계", decision: "결정", control: "제어", blocked: "차단", error: "오류", retry: "재시도", backoff: "호출 제한", recover: "복구", fallback: "대체 업무", inquiry: "문의", release: "출시", settings: "설정", warn: "경고" };
const DEPT = { planning: "기획", design: "디자인", development: "개발", qa: "검증", release: "출시·운영" };

async function refresh() {
  let st;
  try { st = await api("/api/state" + (projectId ? "?project=" + projectId : "")); }
  catch (e) {
    document.querySelector(".controls").hidden = true;
    $("#banners").innerHTML = `<div class="banner err">관리 화면에 연결하지 못했습니다: ${esc(e.message)}<br>AI팀이 실행 중인지 확인하고 <b>open-dashboard.bat</b>으로 다시 여세요.</div>`;
    return;
  }
  document.querySelector(".controls").hidden = false;
  S = st; if (st.project) projectId = st.project.id;
  renderTop(); renderBanners(); renderKpis(); renderDecisions(); renderPipeline(); renderPanel();
}

function renderTop() {
  const sel = $("#projSel");
  const opts = S.projects.map((p) => `<option value="${p.id}" ${p.id === projectId ? "selected" : ""}>${esc(p.name)}${p.is_demo ? " [모의]" : ""}</option>`).join("");
  if (sel.dataset.k !== opts) { sel.innerHTML = opts || "<option>프로젝트 없음</option>"; sel.dataset.k = opts; }
  const p = S.project;
  $("#projGoal").innerHTML = p ? `${esc(p.stage)} · v${esc(p.app_version)} · ${esc(p.goal)}${p.paused ? ' · <b>프로젝트 일시 중지</b>' : ""}` : "아직 프로젝트가 없습니다";
  const ts = $("#teamState"); ts.textContent = "팀: " + S.team.state_ko;
  ts.className = "pill " + ({ running: "ok", paused: "warn", emergency: "err", stopped: "" }[S.team.state] || "");
  $("#btnStart").hidden = S.team.state !== "stopped";
  $("#btnPause").hidden = S.team.state !== "running";
  $("#btnResume").hidden = !["paused", "emergency"].includes(S.team.state);
}

function renderBanners() {
  const t = S.team, b = [];
  if (!t.paid_ready && t.runner !== "mock") b.push(`<div class="banner warn"><b>인증 방식과 하루·월 예산이 아직 정해지지 않았습니다.</b> 정하기 전에는 실제 모델 호출(유료)을 하지 않습니다. 모의 데모는 바로 써 볼 수 있습니다. <button class="small" onclick="openSettings()">설정 열기</button></div>`);
  if (t.runner === "mock") b.push(`<div class="banner warn">전체가 <b>모의 호출 모드</b>입니다. 결과는 실제 모델이 만든 것이 아닙니다.</div>`);
  if (S.project && S.project.is_demo) b.push(`<div class="banner warn">이 프로젝트는 <b>모의 데모</b>입니다. 모델 결과는 미리 만든 예시이며, 파일 적용·검증 명령·상태 전환은 실제로 실행됩니다.</div>`);
  if (t.auth_problem) b.push(`<div class="banner err"><b>인증 문제</b>로 실제 호출을 멈췄습니다: ${esc(t.auth_problem)}</div>`);
  if (t.paid_hold) b.push(`<div class="banner err">사용 한도 소진으로 실제 호출을 잠시 멈췄습니다(30분 후 재시도).</div>`);
  if (t.gate.level !== "ok" && t.paid_ready) b.push(`<div class="banner ${t.gate.level === "stop" ? "err" : "warn"}">${esc(t.gate.reason)}</div>`);
  if (S.disk.warning) b.push(`<div class="banner warn">저장 공간: ${esc(S.disk.warning)}</div>`);
  $("#banners").innerHTML = b.join("");
}

function renderKpis() {
  const u = S.team.usage, bd = S.team.budget;
  const day = u.day, mon = u.month;
  const collecting = day.collecting ? ' <span class="pill run">집계 중</span>' : "";
  const tok = day.calls ? `${num(day.inp)} / ${num(day.outp)}` : NA;
  const pend = S.decisions.filter((d) => d.kind !== "user_action").length, ua = S.decisions.filter((d) => d.kind === "user_action").length;
  $("#kpis").innerHTML = `
  <div class="kpi"><div class="t">오늘 추정 API 비용 / 하루 예산</div><div class="v">${day.calls ? usd(day.cost) : NA}${collecting}</div><div class="s">예산 ${bd.daily_usd == null ? "미설정" : "$" + bd.daily_usd} · 실제 호출 ${day.calls || 0}회${day.missing ? ` · 미수집 ${day.missing}건` : ""}</div></div>
  <div class="kpi"><div class="t">이번 달 추정 비용 / 월 예산</div><div class="v">${mon.calls ? usd(mon.cost) : NA}</div><div class="s">예산 ${bd.monthly_usd == null ? "미설정" : "$" + bd.monthly_usd} · 실제 청구액은 미수집(청구서 확인)</div></div>
  <div class="kpi"><div class="t">오늘 토큰 입력 / 출력</div><div class="v" style="font-size:16px">${tok}</div><div class="s">캐시 읽기 ${day.calls ? num(day.cr) : "미수집"} · 쓰기 ${day.calls ? num(day.cw) : "미수집"}${S.team.auth_mode === "subscription" ? " · 구독 한도 잔여: 미수집" : ""}</div></div>
  <div class="kpi"><div class="t">내가 할 일 / 승인 대기</div><div class="v">${ua} / ${pend}</div><div class="s">${S.decisions.some((d) => d.long_wait) ? `${S.team.decision_wait_hours}시간 넘은 요청 있음 · 팀은 다른 업무 진행` : "응답이 늦어도 팀은 다른 일을 합니다"}</div></div>
  <div class="kpi"><div class="t">${esc(S.disk.drive || "")} 드라이브 여유 공간</div><div class="v">${S.disk.free_gb == null ? NA : S.disk.free_gb + " GB"}</div><div class="s" title="${esc(S.disk.path)}">${esc(S.disk.path)}</div></div>`;
}

function uaBlock(b) {
  const cost = b.cost ? `${esc(b.cost.value)} <span class="pill">${{ confirmed: "확인된 값", estimate: "추정", unknown: "미확인" }[b.cost.kind] || ""}</span>` : "미확인";
  return `<dl class="ua"><dt>할 일</dt><dd><b>${esc(b.todo)}</b></dd><dt>지금 필요한 이유</dt><dd>${esc(b.why_now)}</dd>
  <dt>공식 사이트·설정 위치</dt><dd>${esc(b.where)}</dd><dt>따라 할 순서</dt><dd><ol style="margin:0;padding-left:18px">${(b.steps || []).map((s) => `<li>${esc(s)}</li>`).join("")}</ol></dd>
  <dt>비용</dt><dd>${cost}</dd><dt>완료 후 알려줄 내용</dt><dd>${esc(b.report_back)}</dd><dt>이 일 때문에 대기하는 작업</dt><dd>${esc(b.blocked_work)}</dd><dt>그동안 팀이 진행할 일</dt><dd>${esc(b.meanwhile)}</dd></dl>
  <p class="small muted">API 키·비밀번호는 여기나 채팅에 붙여 넣지 마세요. README의 '비밀정보 넣는 법'을 따르세요.</p>`;
}

function renderDecisions() {
  const key = JSON.stringify(S.decisions.map((d) => [d.id, d.long_wait]));
  if (key === lastDecisionKey) return; lastDecisionKey = key;
  const el = $("#decisions");
  if (!S.decisions.length) { el.innerHTML = `<div class="empty">지금 결정하거나 직접 할 일이 없습니다.</div>`; return; }
  el.innerHTML = S.decisions.map((d) => {
    const b = d.body, head = `<div class="row"><h3>${esc(d.title)}</h3><span class="small muted">요청 ${time(d.requested_at)} · ${d.age_h ?? 0}시간 경과${d.long_wait ? " · <b>사용자 결정 대기 유지(자동 승인 안 함)</b>" : ""}${d.blocks ? " · 대기 작업: " + esc(d.blocks) : ""}</span></div>`;
    let inner = "";
    if (d.kind === "mvp_direction") {
      inner = `<p>${esc(b.summary)}</p>
      <div class="grid2"><div><b class="small">포함 범위</b><ul>${b.mvp_scope.included.map((x) => `<li>${esc(x)}</li>`).join("")}</ul><b class="small">제외</b><ul>${b.mvp_scope.excluded.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
      <div><b class="small">완료 기준</b><ul>${b.done_criteria.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div></div>
      <b class="small">방향 선택</b>${b.directions.map((x) => `<label class="opt"><input type="radio" name="dir${d.id}" value="${esc(x.id)}" ${x.recommended ? "checked" : ""}><span><b>${esc(x.title)}</b> ${x.recommended ? '<span class="pill ok">추천</span>' : ""}<br>${esc(x.description)}<br><span class="small muted">장단점: ${esc(x.tradeoffs)}</span></span></label>`).join("")}
      ${b.questions.map((q, i) => `<label for="q${d.id}_${i}">${esc(q.question)} <span class="muted">(${esc(q.why)}) · 추천: ${esc(q.recommendation)}</span></label><select id="q${d.id}_${i}">${q.options.map((o) => `<option ${o === q.recommendation ? "selected" : ""}>${esc(o)}</option>`).join("")}<option value="">모르겠음(추천대로)</option></select>`).join("")}
      <label for="note${d.id}">추가 메모(선택)</label><textarea id="note${d.id}"></textarea>
      <div class="flex"><button class="primary" onclick="answerMvp(${d.id},${b.questions.length})">이 방향으로 진행</button></div>`;
    } else if (d.kind === "design_direction") {
      inner = `<p>${esc(b.summary)}</p>${b.options.map((o) => `<label class="opt"><input type="radio" name="dd${d.id}" value="${esc(o.id)}"><span><b>${esc(o.title)}</b><br>${esc(o.description)}<br><a href="#" onclick="showPreview('/artifact/${esc(o.preview)}');return false">시안 보기</a></span></label>`).join("")}
      <button class="primary" onclick="answerDD(${d.id})">이 시안으로 진행</button>`;
    } else if (d.kind === "deploy_approval") {
      inner = `<div class="grid2"><div><b class="small">검증을 통과한 기능(홍보에 사용)</b><ul>${b.verified_features.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      ${b.dropped_claims.length ? `<b class="small">검증되지 않아 소개에서 뺀 문구</b><ul>${b.dropped_claims.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}</div>
      <div><b class="small">배포 순서</b><ol>${b.deploy_plan.map((x) => `<li>${esc(x)}</li>`).join("")}</ol><b class="small">복구 방법</b><ol>${b.rollback_plan.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>
      <p class="small muted">배포 명령: ${b.deploy_command ? `<code>${esc(b.deploy_command)}</code> (승인 시 실행)` : "없음 — 승인하면 직접 배포 안내가 할 일로 생깁니다"}</p></div></div>
      <div class="flex"><button class="primary" onclick="act('/api/decisions/${d.id}/answer',{approve:true},'배포 승인')">배포 승인</button><button onclick="act('/api/decisions/${d.id}/answer',{approve:false},'보류')">보류</button></div>`;
    } else if (d.kind === "qa_escalation") {
      inner = `<p class="small">${esc(b.evidence)}</p><ul>${b.findings.map((f) => `<li>[${SEV[f.severity] || f.severity}] ${esc(f.description)}</li>`).join("")}</ul>
      <div class="flex"><button class="primary" onclick="act('/api/decisions/${d.id}/answer',{choice:'다시 시도'})">다시 시도</button><button onclick="act('/api/decisions/${d.id}/answer',{choice:'보류'})">이 작업 보류</button></div>`;
    } else if (d.kind === "proposal_selection") {
      inner = `<p>개선안을 확인하고 각 안을 선택·보류·거절하세요. 선택하지 않은 안은 개발하지 않습니다.</p><button class="primary" onclick="setTab('proposals')">개선안 보기</button>`;
    } else if (d.kind === "user_action") {
      inner = uaBlock(b) + `<button class="primary" onclick="act('/api/decisions/${d.id}/answer',{done:true},'완료로 기록')">완료</button>`;
    }
    return `<div class="item decision ${d.kind === "user_action" ? "ua" : ""}">${d.kind === "user_action" ? '<span class="pill warn">내가 해야 할 일</span>' : '<span class="pill wait">승인·선택</span>'}${head}${inner}</div>`;
  }).join("");
}
window.answerMvp = (id, nq) => {
  const r = document.querySelector(`input[name=dir${id}]:checked`); if (!r) return alert("방향을 고르세요");
  const answers = {}; for (let i = 0; i < nq; i++) answers[i] = $(`#q${id}_${i}`).value || "추천대로";
  act(`/api/decisions/${id}/answer`, { direction_id: r.value, answers, note: $(`#note${id}`).value }, "기획 방향을 전달했습니다");
};
window.answerDD = (id) => { const r = document.querySelector(`input[name=dd${id}]:checked`); if (!r) return alert("시안을 고르세요"); act(`/api/decisions/${id}/answer`, { option_id: r.value }, "디자인 방향 확정"); };

function renderPipeline() {
  const recent = {};
  S.handoffs.forEach((h) => { if (h.age_s < 25) recent[h.dept] = true; });
  $("#pipeline").innerHTML = S.departments.map((d, i) => {
    const u = d.usage, nextKey = S.departments[i + 1] && S.departments[i + 1].key;
    const use = u.collecting ? '<span class="pill run">집계 중</span>' : (u.collected ? `토큰 입력 ${num(u.input)} · 출력 ${num(u.output)} · 캐시 ${num((u.cache_read || 0) + (u.cache_write || 0))} · ${usd(u.cost)}` : (u.mock_calls ? `모의 호출 ${u.mock_calls}회(사용량 없음)` : "오늘 실제 호출 없음"));
    const pill = { idle: "", running: "run", waiting_user: "wait", paused: "warn", error: "err" }[d.status];
    return `<div class="card ${d.status}">
      <div class="head"><span><span class="num">${d.order}</span><span class="name">${esc(d.name)}</span></span><span class="pill ${pill}"><span class="dot"></span>${esc(d.status_ko)}</span></div>
      ${d.mock && d.status === "running" ? '<span class="pill mock">모의 호출</span>' : ""}
      <div class="cur">${d.current ? `<span class="small muted">${esc(d.current_kind)}:</span> ${esc(d.current)}` : '<span class="muted">할 일이 없어 모델을 부르지 않고 기다립니다</span>'}
      ${d.waiting.length ? `<div class="small" style="color:var(--wait)">답변 대기: ${d.waiting.map(esc).join(", ")}</div>` : ""}</div>
      <div class="meta">최근 결과: ${d.recent ? `${esc(TSTATUS[d.recent.status] || d.recent.status)} — ${esc(d.recent.title)}${d.recent.summary ? `<br>${esc(String(d.recent.summary).slice(0, 140))}` : ""}` : "없음"}</div>
      <div class="meta">다음 전달: ${esc(d.next)}</div>
      <div class="usage-line">오늘: ${use}</div>
      <div class="flex">${d.paused ? `<button class="small" onclick="act('/api/control',{action:'resume',scope:'dept',target:'${d.key}'})">부서 재개</button>` : `<button class="small" onclick="act('/api/control',{action:'pause',scope:'dept',target:'${d.key}'})">부서 중지</button>`}</div>
      ${i < 4 ? `<div class="arrow ${recent[nextKey] ? "active" : ""}" aria-hidden="true"></div>` : ""}
    </div>`;
  }).join("");
}

window.setTab = (t) => { tab = t; lastPanelKey = ""; document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t)); renderPanel(); };
document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));

function renderPanel() {
  const data = { timeline: S.timeline, inquiries: [S.inquiries, S.clusters], proposals: S.proposals, preview: [S.artifacts, previewPath, S.project && S.project.slug], qa: [S.qa, S.artifacts, S.releases], queue: [S.tasks, S.decisions] }[tab];
  const key = tab + JSON.stringify(data);
  if (key === lastPanelKey) return;
  if (document.activeElement && $("#panel").contains(document.activeElement) && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  lastPanelKey = key;
  $("#panel").innerHTML = ({ timeline: pTimeline, inquiries: pInquiries, proposals: pProposals, preview: pPreview, qa: pQa, queue: pQueue })[tab]();
}

function pTimeline() {
  if (!S.timeline.length) return `<div class="empty">아직 실행 기록이 없습니다.</div>`;
  return `<div class="tl">${S.timeline.map((e) => `<span class="muted">${hm(e.ts)}</span><span class="k">${esc(DEPT[e.dept] || "전체")} · ${esc(EVK[e.kind] || e.kind)}</span><span>${e.is_mock ? '<span class="pill mock">모의</span> ' : ""}${esc(e.message)}</span>`).join("")}</div>`;
}

function pInquiries() {
  const slug = S.project && S.project.slug;
  const supportUrl = slug ? `http://${S.team.ports.public_bind}:${S.team.ports.public}/support?p=${encodeURIComponent(slug)}` : "";
  const cl = S.clusters.map((c) => `<div class="item"><div class="row"><h3>${esc(c.title)}</h3><span class="flex"><span class="pill">${esc(KIND[c.kind] || c.kind)}</span><span class="pill ${c.severity === "critical" || c.severity === "high" ? "err" : ""}">심각도 ${esc(SEV[c.severity] || c.severity)}</span><span class="pill">${esc({ open: "열림", in_progress: "개선 진행", deployed: "배포 완료(해결 확인 전)", resolved_confirmed: "해결 확인됨" }[c.status] || c.status)}</span></span></div>
    <p class="small">문의 ${c.stats.count}건 · 서로 다른 고객 ${c.stats.customers}명 · 최근 7일 ${c.stats.last7}건 / 이전 7일 ${c.stats.prev7}건 (${esc(c.stats.trend)}) <span class="muted">— 일부 문의이며 전체 고객 의견이 아닙니다</span></p>
    <div class="grid2"><div><b class="small">고객 표현</b><ul>${c.quotes.map((q) => `<li>“${esc(q)}”</li>`).join("")}</ul><b class="small">고객이 요청한 기능</b><p>${esc(c.requested_feature)}</p><b class="small">해결하려는 문제</b><p>${esc(c.underlying_problem)}</p></div>
    <div><b class="small">확인된 사실</b><ul>${c.facts.map((q) => `<li>${esc(q)}</li>`).join("")}</ul><b class="small">팀의 추정</b><ul>${c.estimates.map((q) => `<li>${esc(q)}</li>`).join("")}</ul><b class="small">영향</b><p>${esc(c.impact)}</p></div></div>
    <details><summary>원본 문의 ${c.inquiry_ids.length}건 · 답변 초안(자동 전송 꺼짐)</summary><p class="small">${c.inquiry_ids.map(esc).join(", ")}</p><p class="small">${esc(c.reply_draft)}</p></details>
    ${c.status === "deployed" ? `<button class="small" onclick="act('/api/clusters/${c.id}/confirm',{},'해결 확인으로 기록')">고객 문제 해결 확인</button>` : ""}</div>`).join("");
  const rows = S.inquiries.map((i) => `<tr><td>${esc(i.receipt_no)}<br><span class="muted">${time(i.created_at)}</span></td><td>${esc(i.category_ko)}</td><td>${esc(i.content)}${i.expected ? `<br><span class="muted">기대: ${esc(i.expected)}</span>` : ""}</td><td class="small">v${esc(i.app_version)} · ${esc(i.screen)}<br>${esc(i.device)}${i.attachments.length ? `<br>${i.attachments.map((a, k) => `<a href="/api/inquiries/${i.id}/attachment/${k}">첨부${k + 1}</a>`).join(" ")}` : ""}</td><td>${esc(i.status_ko)}${i.repeat_of_customer ? '<br><span class="pill">같은 고객 반복</span>' : ""}${i.contact ? '<br><span class="small muted">연락처 있음</span>' : ""}</td><td>${i.cluster_id ? "C" + i.cluster_id : '<span class="muted">미분석</span>'}</td></tr>`).join("");
  return `<p class="small muted">고객 문의 화면: ${supportUrl ? `<a href="${esc(supportUrl)}" target="_blank" rel="noopener">${esc(supportUrl)}</a>` : "-"} · 문의는 출시(운영 중) 이후 분석합니다. 분석은 새 문의가 ${esc(3)}건 이상 쌓이거나 하루가 지나면 실행됩니다.</p>
  <h2>문제별 묶음</h2>${cl || '<div class="empty">아직 묶음이 없습니다.</div>'}
  <h2>문의 원본</h2>${rows ? `<div style="overflow-x:auto"><table><tr><th>접수 번호</th><th>분류</th><th>내용</th><th>재현 정보</th><th>상태</th><th>묶음</th></tr>${rows}</table></div>` : '<div class="empty">접수된 문의가 없습니다.</div>'}`;
}

function pProposals() {
  if (!S.proposals.length) return `<div class="empty">아직 개선안이 없습니다. 출시 후 문의가 모이면 기획 부서가 최대 3개를 제시합니다.</div>`;
  return S.proposals.map((p) => { const b = p.body;
    const ev = p.evidence.map((e) => `C${e.cluster_id}: 문의 ${e.count}건·고객 ${e.customers}명·최근 7일 ${e.last7}건`).join(" / ");
    const can = ["proposed", "held"].includes(p.status);
    return `<div class="item"><div class="row"><h3>${esc(p.title)}</h3><span class="flex"><span class="pill">규모 ${esc(SIZE[p.size] || p.size)}</span><span class="pill ${p.status === "selected" || p.status === "in_progress" ? "run" : p.status === "deployed" ? "ok" : ""}">${esc(PSTATUS[p.status] || p.status)}</span></span></div>
    <dl class="ua"><dt>해결할 문제</dt><dd>${esc(b.problem)}</dd><dt>근거 문의(코드 집계)</dt><dd>${esc(ev)}</dd><dt>고객 기대 결과</dt><dd>${esc(b.expected_outcome)}</dd>
    <dt>제안 변경</dt><dd>${esc(b.change)}</dd><dt>대안</dt><dd>${(b.alternatives || []).map(esc).join(" / ")}</dd><dt>기대 효과·확인 방법</dt><dd>${esc(b.effect_and_measure)}</dd>
    <dt>예상 비용·불확실성</dt><dd>${esc(b.cost_and_uncertainty)}</dd><dt>외부 설정·위험</dt><dd>${esc(b.external_setup_and_risks)}</dd><dt>추천 이유</dt><dd>${esc(b.recommendation_reason)}</dd>
    ${b.strengthened ? `<dt>근거 보강</dt><dd>${(b.strengthened.additional_evidence || []).map(esc).join(" / ")}</dd>` : ""}
    ${p.scope_note ? `<dt>선택한 실행 범위</dt><dd>${esc(p.scope_note)}</dd>` : ""}</dl>
    ${can ? `<label for="scope${p.id}">실행 범위 메모(선택)</label><input id="scope${p.id}" placeholder="예: 목록 화면의 수정 버튼만">
    <div class="flex" style="margin-top:8px"><button class="primary" onclick="decide(${p.id},'select')">선택</button><button onclick="decide(${p.id},'hold')">보류</button><button onclick="decide(${p.id},'reject')">거절</button></div>` : `<p class="small muted">결정 ${time(p.decided_at)}</p>`}</div>`; }).join("");
}
window.decide = (id, c) => act(`/api/proposals/${id}/decide`, { choice: c, scope: ($("#scope" + id) || {}).value || "" }, { select: "선택: 디자인 부서로 전달", hold: "보류", reject: "거절" }[c]);

window.showPreview = (p) => { previewPath = p; setTab("preview"); };
function pPreview() {
  const prevs = S.artifacts.filter((a) => a.kind === "preview");
  const slug = S.project && S.project.slug;
  const appUrl = slug ? `http://127.0.0.1:${S.team.ports.preview}/${encodeURIComponent(slug)}/${encodeURIComponent((S.project_settings.preview_path || "index.html"))}` : null;
  const cur = previewPath || (prevs[0] && "/artifact/" + prevs[0].path);
  return `<div class="flex"><b>디자인 미리보기:</b>${prevs.map((a) => `<button class="small" onclick="showPreview('/artifact/${esc(a.path)}')">${esc(a.title)} #${a.task_id}</button>`).join("") || '<span class="muted">아직 없음</span>'}
    ${appUrl ? `<a class="right" href="${esc(appUrl)}" target="_blank" rel="noopener">실제 앱 미리보기 새 창 열기</a>` : ""}</div>
    <p class="small muted">미리보기는 스크립트 없이 격리해서 보여줍니다. 실제 앱은 관리 화면과 분리된 주소(포트 ${esc(S.team.ports.preview)})에서 열립니다.</p>
    ${cur ? `<iframe class="preview" sandbox="" src="${esc(cur)}" title="디자인 미리보기"></iframe>` : '<div class="empty">디자인 부서가 미리보기를 만들면 여기에 보입니다.</div>'}`;
}

function pQa() {
  const q = S.qa.map((r) => `<div class="item"><div class="row"><h3>검증 #${r.task_id}</h3><span class="pill ${r.verdict === "pass" ? "ok" : "err"}">${r.verdict === "pass" ? "통과" : "실패"}</span></div>
    <p class="small">${esc(r.summary)}</p><p class="small muted">증거: ${esc(r.evidence)}</p>
    ${r.checks.length ? `<table><tr><th>검증 명령(실제 실행)</th><th>결과</th><th>시간</th></tr>${r.checks.map((c) => `<tr><td><code>${esc(c.command)}</code></td><td>${c.ok ? "통과" : "실패"} (exit ${esc(c.exit)})</td><td>${esc(c.seconds)}초</td></tr>`).join("")}</table>` : ""}
    ${r.findings.length ? `<ul>${r.findings.map((f) => `<li>[${SEV[f.severity] || f.severity}·${esc(DEPT[f.area] || f.area)}] ${esc(f.description)}</li>`).join("")}</ul>` : ""}</div>`).join("");
  const rel = S.releases.map((r) => `<li>v${esc(r.version)} — ${esc({ prepared: "준비됨", approved: "승인됨(배포 전)", deployed: "배포 완료", held: "보류" }[r.status] || r.status)} ${r.deployed_at ? time(r.deployed_at) : ""}</li>`).join("");
  const arts = S.artifacts.map((a) => `<tr><td>${time(a.created_at)}</td><td>${esc(DEPT[a.dept] || a.dept)}</td><td><a href="/artifact/${esc(a.path)}" target="_blank" rel="noopener">${esc(a.title)}</a></td><td class="small muted">artifacts/${esc(a.path)}</td></tr>`).join("");
  return `<h2>검증 결과</h2>${q || '<div class="empty">아직 검증 기록이 없습니다.</div>'}<h2>출시 기록</h2>${rel ? `<ul>${rel}</ul>` : '<div class="empty">없음</div>'}
  <h2>결과물</h2>${arts ? `<div style="overflow-x:auto"><table><tr><th>시각</th><th>부서</th><th>결과물</th><th>위치</th></tr>${arts}</table></div>` : '<div class="empty">없음</div>'}`;
}

function pQueue() {
  const waiting = S.decisions.map((d) => `<li>${esc(d.title)} — ${d.age_h ?? 0}시간 대기${d.blocks ? ` · 막힌 작업: ${esc(d.blocks)}` : ""}</li>`).join("");
  const fb = S.tasks.filter((t) => t.is_fallback);
  const row = (t) => `<tr><td>#${t.id}</td><td>${esc(DEPT[t.dept])}</td><td>${esc(t.title)}${t.done_criteria ? `<br><span class="small muted">완료 기준: ${esc(t.done_criteria)}</span>` : ""}</td><td>${esc(TSTATUS[t.status] || t.status)}${t.blocked_reason ? `<br><span class="small" style="color:var(--warn)">${esc(t.blocked_reason)}</span>` : ""}${t.note ? `<br><span class="small muted">${esc(t.note)}</span>` : ""}${t.last_error && t.status !== "done" ? `<br><span class="small" style="color:var(--err)">${esc(String(t.last_error).slice(0, 160))}</span>` : ""}</td><td class="small">우선순위 ${t.priority} · 시도 ${t.attempts}<br>한도 ${t.time_limit_sec ? Math.round(t.time_limit_sec / 60) + "분" : "-"} · ${t.token_limit ? num(t.token_limit) + " 토큰" : "-"}</td><td>${["failed", "needs_verification"].includes(t.status) ? `<button class="small" onclick="act('/api/tasks/${t.id}/retry',{},'다시 시도')">다시 시도</button>` : ""}</td></tr>`;
  const head = `<tr><th>ID</th><th>부서</th><th>작업</th><th>상태</th><th>한도</th><th></th></tr>`;
  const act_ = S.tasks.filter((t) => ["queued", "running", "failed", "needs_verification"].includes(t.status));
  return `<h2>사용자 답변을 기다리는 일</h2>${waiting ? `<ul>${waiting}</ul>` : '<div class="empty">없음</div>'}
  <h2>그동안 대신 진행하는 일</h2><p class="small muted">결정 요청 후 ${esc(S.team.decision_wait_hours)}시간이 지나도 답이 없으면, 실행 가능한 다른 업무를 우선순위대로 고릅니다. 시간이 지났다는 이유로 구매·가입·선택·배포를 대신 결정하지 않습니다.</p>
  ${fb.length ? `<table>${head}${fb.map(row).join("")}</table>` : '<div class="empty">없음</div>'}
  <h2>진행 중·대기열·실패</h2>${act_.length ? `<div style="overflow-x:auto"><table>${head}${act_.map(row).join("")}</table></div>` : '<div class="empty">실행할 일이 없어 토큰을 쓰지 않고 기다리는 중입니다.</div>'}
  <h2>최근 완료</h2><div style="overflow-x:auto"><table>${head}${S.tasks.filter((t) => t.status === "done" || t.status === "cancelled").slice(0, 20).map(row).join("")}</table></div>`;
}

// ───── 상단 제어 ─────
$("#btnStart").onclick = () => act("/api/control", { action: "start" }, "전체 시작");
$("#btnPause").onclick = () => act("/api/control", { action: "pause" }, "새 작업 배정을 멈췄습니다. 진행 중인 작업은 안전한 지점까지 마무리합니다");
$("#btnResume").onclick = () => act("/api/control", { action: "resume" }, "재개");
$("#btnEmergency").onclick = () => { if (confirm("실행 중인 모델과 도구 작업을 즉시 중단합니다. 부분 변경은 보존되고 '검증 필요'로 표시됩니다. 계속할까요?")) act("/api/control", { action: "emergency_stop" }, "긴급 중지"); };
$("#projSel").onchange = (e) => { projectId = Number(e.target.value); lastDecisionKey = lastPanelKey = ""; previewPath = null; refresh(); };
$("#newProjBtn").onclick = () => $("#projModal").classList.add("on");
$("#pmCancel").onclick = () => $("#projModal").classList.remove("on");
$("#pmOk").onclick = async () => {
  try {
    const r = await api("/api/projects", { name: $("#pmName").value, idea: $("#pmIdea").value, customers: $("#pmCust").value, source_path: $("#pmSrc").value, demo: $("#pmDemo").checked });
    projectId = r.result.project_id; $("#projModal").classList.remove("on"); $("#pmErr").textContent = ""; refresh();
  } catch (e) { $("#pmErr").textContent = e.message; }
};

window.openSettings = () => {
  const t = S.team, b = t.budget, ps = S.project_settings || {};
  const m = (k) => `<label for="m_${k}">${DEPT[k]} 모델</label><input id="m_${k}" value="${esc(t.models[k])}">`;
  $("#setBox").innerHTML = `<h2 id="smTitle">설정</h2>
  <p class="small muted">설정은 내 PC의 state/settings.json에 저장됩니다. AI 작업자는 이 설정을 바꿀 수 없습니다. API 키는 여기에 넣지 않습니다.</p>
  <label for="s_auth">Claude 인증 방식</label><select id="s_auth"><option value="">선택 안 함(실제 호출 안 함)</option><option value="subscription" ${t.auth_mode === "subscription" ? "selected" : ""}>구독 로그인(Pro/Max) — claude 로그인 사용</option><option value="api_key" ${t.auth_mode === "api_key" ? "selected" : ""}>API 키 — 환경 변수 ANTHROPIC_API_KEY ${t.api_key_env ? "(설정됨)" : "(없음)"}</option></select>
  <div class="grid2"><div><label for="s_day">하루 예산(USD, 추정 API 비용 기준)</label><input id="s_day" type="number" min="0" step="0.5" value="${b.daily_usd ?? ""}"></div>
  <div><label for="s_mon">월 예산(USD)</label><input id="s_mon" type="number" min="0" step="1" value="${b.monthly_usd ?? ""}"></div>
  <div><label for="s_call">호출 1회 상한(USD)</label><input id="s_call" type="number" min="0.05" step="0.05" value="${b.per_call_usd ?? ""}"></div>
  <div><label for="s_tok">하루 토큰 한도(선택)</label><input id="s_tok" type="number" min="0" step="10000" value="${b.daily_tokens ?? ""}"></div></div>
  <div class="grid2">${["planning", "design", "development", "qa", "release"].map(m).join("")}</div>
  <label for="s_runner">호출 방식</label><select id="s_runner"><option value="claude-cli" ${t.runner === "claude-cli" ? "selected" : ""}>실제 호출(Claude Code CLI)</option><option value="mock" ${t.runner === "mock" ? "selected" : ""}>전체 모의 호출(비용 없음)</option></select>
  <label for="s_wait">결정 대기 후 다른 업무 선택까지(시간)</label><input id="s_wait" type="number" min="0" value="${t.decision_wait_hours}">
  <label class="flex"><input type="checkbox" id="s_bug" style="width:auto" ${t.auto_bugfix ? "checked" : ""}> 사전 승인: 재현 가능한 버그 수정은 선택 없이 진행(배포는 여전히 승인 필요)</label>
  <p class="small muted">고객 답변 자동 전송: ${t.reply_auto_send ? "켜짐" : "꺼짐"}(기본). 특허 준비 기능: ${t.patent_prep ? "켜짐" : "선택 기능 — 꺼짐"}.</p>
  ${S.project ? `<h2>프로젝트 설정: ${esc(S.project.name)}</h2>
  <label for="p_checks">검증 명령(한 줄에 하나, 허용 목록 안에서만)</label><textarea id="p_checks">${esc((ps.checks || []).join("\n"))}</textarea>
  <p class="small muted">허용 목록: ${S.allowlist.map((a) => `<code>${esc(a)}</code>`).join(" ")}</p>
  <label for="p_deploy">배포 명령(선택, 승인 후에만 실행)</label><input id="p_deploy" value="${esc(ps.deploy_command || "")}">
  <label for="p_prev">앱 미리보기 시작 파일</label><input id="p_prev" value="${esc(ps.preview_path || "index.html")}">
  <div class="flex" style="margin-top:8px">${S.project.paused ? `<button class="small" onclick="act('/api/control',{action:'resume',scope:'project',target:${S.project.id}})">프로젝트 재개</button>` : `<button class="small" onclick="act('/api/control',{action:'pause',scope:'project',target:${S.project.id}})">프로젝트 일시 중지</button>`}</div>` : ""}
  <div class="flex" style="margin-top:14px"><button class="primary" onclick="saveSettings()">저장</button><button onclick="$('#setModal').classList.remove('on')">닫기</button></div><p id="setErr" class="small" style="color:var(--err)"></p>`;
  $("#setModal").classList.add("on");
};
$("#btnSettings").onclick = () => openSettings();
const nOrNull = (v) => (v === "" ? null : Number(v));
window.saveSettings = async () => {
  try {
    await api("/api/settings", {
      auth: { mode: $("#s_auth").value || null }, runner: $("#s_runner").value,
      budget: { daily_usd: nOrNull($("#s_day").value), monthly_usd: nOrNull($("#s_mon").value), per_call_usd: nOrNull($("#s_call").value), daily_tokens: nOrNull($("#s_tok").value) },
      models: Object.fromEntries(["planning", "design", "development", "qa", "release"].map((k) => [k, $("#m_" + k).value.trim()])),
      decision_wait_hours: Number($("#s_wait").value), maintenance: { auto_bugfix: $("#s_bug").checked },
    });
    if (S.project) await api(`/api/projects/${S.project.id}/settings`, { checks: $("#p_checks").value.split("\n"), deploy_command: $("#p_deploy").value, preview_path: $("#p_prev").value });
    $("#setModal").classList.remove("on"); toast("설정을 저장했습니다"); refresh();
  } catch (e) { $("#setErr").textContent = e.message; }
};

(async () => { await login(); await refresh(); setInterval(() => { if (!document.hidden) refresh(); }, 2500); })();
