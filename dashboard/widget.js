/* 앱에 붙이는 문의하기 버튼. 사용법:
   <script src="http://주소:8790/widget.js" data-project="프로젝트" data-version="1.0.0" defer></script> */
(function () {
  var me = document.currentScript || document.querySelector('script[src*="widget.js"]');
  if (!me) return;
  var base = new URL(me.src).origin;
  var project = me.getAttribute("data-project") || "";
  var version = me.getAttribute("data-version") || "";
  var btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = "문의하기";
  btn.setAttribute("aria-haspopup", "dialog");
  btn.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483000;min-height:48px;padding:0 18px;border:0;border-radius:24px;background:#3657d6;color:#fff;font:600 15px system-ui,'Malgun Gothic',sans-serif;box-shadow:0 4px 14px rgba(0,0,0,.2);cursor:pointer";
  var wrap = null;
  function close() { if (wrap) { wrap.remove(); wrap = null; btn.focus(); } }
  btn.onclick = function () {
    if (wrap) return close();
    wrap = document.createElement("div");
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-label", "문의하기");
    wrap.style.cssText = "position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.4);display:flex;align-items:flex-end;justify-content:center";
    var frame = document.createElement("iframe");
    frame.title = "문의하기";
    frame.src = base + "/support?p=" + encodeURIComponent(project) + "&v=" + encodeURIComponent(version) + "&screen=" + encodeURIComponent(location.pathname);
    frame.style.cssText = "width:100%;max-width:560px;height:88vh;border:0;border-radius:16px 16px 0 0;background:#fff";
    var x = document.createElement("button");
    x.type = "button"; x.textContent = "닫기";
    x.style.cssText = "position:absolute;top:12px;right:12px;min-height:44px;padding:0 14px;border:0;border-radius:10px;background:#fff;color:#1d2330;font:600 14px system-ui,sans-serif;cursor:pointer";
    x.onclick = close;
    wrap.onclick = function (e) { if (e.target === wrap) close(); };
    document.addEventListener("keydown", function k(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", k); } });
    wrap.appendChild(frame); wrap.appendChild(x); document.body.appendChild(wrap); x.focus();
  };
  if (document.body) document.body.appendChild(btn); else document.addEventListener("DOMContentLoaded", function () { document.body.appendChild(btn); });
})();
