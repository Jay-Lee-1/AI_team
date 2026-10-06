"use strict";
(function () {
  const q = new URLSearchParams(location.search);
  const project = (q.get("p") || "").toLowerCase();
  const version = q.get("v") || "";
  const screen = q.get("screen") || document.referrer || "";
  const ua = navigator.userAgent;
  const device = (/Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Windows/.test(ua) ? "Windows" : /Mac/.test(ua) ? "macOS" : "기타") +
    " · " + (/Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : /Firefox\//.test(ua) ? "Firefox" : "기타") +
    " · " + (window.innerWidth < 700 ? "모바일 화면" : "넓은 화면");
  let anon = null;
  try { anon = localStorage.getItem("aiteam_anon"); if (!anon) { anon = crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2); localStorage.setItem("aiteam_anon", anon); } } catch (e) { anon = null; }
  const $ = (s) => document.getElementById(s);
  $("diag").textContent = `버전 ${version || "알 수 없음"}, 화면 ${screen || "알 수 없음"}, ${device}`;
  if (!project) { $("msg").textContent = "앱 정보가 없어 문의를 보낼 수 없습니다."; $("send").disabled = true; }
  else fetch(`/api/public/project?p=${encodeURIComponent(project)}`).then((r) => r.json()).then((j) => { if (j.name) $("appName").textContent = j.name + " 문의하기"; }).catch(() => {});

  const toB64 = (file) => new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(",")[1]); r.onerror = no; r.readAsDataURL(file); });
  let sending = false;
  $("f").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (sending) return;
    const msg = $("msg"); msg.className = "";
    if (!$("cat").value) { msg.textContent = "문의 분류를 선택하세요."; msg.className = "err"; $("cat").focus(); return; }
    if ($("content").value.trim().length < 5) { msg.textContent = "무슨 일이 있었는지 5자 이상 적어 주세요."; msg.className = "err"; $("content").focus(); return; }
    const body = { category: $("cat").value, content: $("content").value, expected: $("expected").value, contact: $("contact").value,
      app_version: version, screen, device, anon_id: anon, attachments: [] };
    const f = $("file").files[0];
    if (f) { if (f.size > 2 * 1024 * 1024) { msg.textContent = "첨부는 2MB 이하만 가능합니다."; msg.className = "err"; return; } body.attachments.push({ name: f.name, data: await toB64(f) }); }
    sending = true; $("send").disabled = true; msg.textContent = "보내는 중…";
    try {
      const r = await fetch(`/api/public/inquiries/${encodeURIComponent(project)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "보내지 못했습니다");
      $("f").hidden = true; $("done").hidden = false; $("rno").textContent = j.receipt_no;
      $("rkey").textContent = j.duplicate ? "(방금 보낸 문의와 같아 새로 접수하지 않았습니다)" : j.lookup_key;
    } catch (err) { msg.textContent = err.message + " 잠시 후 다시 시도해 주세요."; msg.className = "err"; }
    finally { sending = false; $("send").disabled = false; }
  });
  $("lb").addEventListener("click", async () => {
    const r = await fetch(`/api/public/status?receipt=${encodeURIComponent($("lr").value.trim())}&key=${encodeURIComponent($("lk").value.trim())}`);
    const j = await r.json().catch(() => ({}));
    $("lmsg").textContent = r.ok ? `${j.receipt_no}: ${j.status}` : (j.error || "확인하지 못했습니다");
  });
})();
