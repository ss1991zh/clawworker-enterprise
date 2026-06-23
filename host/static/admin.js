// Admin 站内信:铃铛 + 未读小红点 + 下拉面板(只读文字,无动作按钮)。
// 每 15s 轮询 /admin/notices.json(读取即同步,天然补发停机期间的信号);打开即全部已读。
(function () {
  "use strict";
  var LV = { info: "提示", warning: "注意", critical: "严重" };
  var state = { items: [], unread: 0, open: false };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtTime(iso) { return (String(iso || "").replace("T", " ").slice(0, 16)) || "—"; }

  function renderDot() {
    var d = $("adminNoticeDot");
    if (d) d.hidden = !(state.unread > 0);
  }

  function renderList() {
    var box = $("adminNoticeList");
    if (!box) return;
    if (!state.items.length) {
      box.innerHTML = '<div class="notice-empty">暂无站内信</div>';
      return;
    }
    box.innerHTML = state.items.map(function (n) {
      var lv = esc(n.level || "info");
      return '<div class="notice-item ' + lv + (n.read ? '' : ' unread') + '">' +
        '<div class="notice-item__t"><span>' + esc(n.title) + '</span>' +
        '<span class="notice-item__lv">' + esc(LV[n.level] || "提示") + '</span></div>' +
        '<div class="notice-item__s">' + esc(n.summary) + '</div>' +
        '<div class="notice-item__time">' + esc(fmtTime(n.created_at)) + '</div>' +
        '</div>';
    }).join("");
  }

  function load() {
    return fetch("/admin/notices.json", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        state.items = d.items || [];
        state.unread = d.unread || 0;
        renderDot();
        if (state.open) renderList();
      })
      .catch(function () { });
  }

  function open() {
    state.open = true;
    $("adminNoticePop").hidden = false;
    $("adminNoticeScrim").hidden = false;
    load().then(function () {
      renderList();
      if (state.unread > 0) {
        fetch("/admin/notices/read", { method: "POST" }).catch(function () { });
        state.unread = 0;
        state.items.forEach(function (n) { n.read = true; });
        renderDot();
        renderList();
      }
    });
  }

  function close() {
    state.open = false;
    $("adminNoticePop").hidden = true;
    $("adminNoticeScrim").hidden = true;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var bell = $("adminNoticeBell");
    if (!bell) return;
    bell.addEventListener("click", function () { state.open ? close() : open(); });
    var x = $("adminNoticeClose"); if (x) x.addEventListener("click", close);
    var scrim = $("adminNoticeScrim"); if (scrim) scrim.addEventListener("click", close);
    load();
    setInterval(load, 15000);
  });
})();
