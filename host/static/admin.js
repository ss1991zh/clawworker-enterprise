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

// 管理端统一交互层：应用内确认/输入弹窗、提交防重复、移动端导航。
(function () {
  "use strict";

  function openDialog(message, options) {
    options = options || {};
    var promptMode = options.kind === "prompt";
    var root = document.createElement("div");
    root.className = "admin-dialog-mask open";
    root.innerHTML = '<section class="admin-dialog ' + (promptMode ? 'prompt' : 'danger') + '" role="dialog" aria-modal="true">' +
      '<div class="admin-dialog__head"><div class="admin-dialog__icon">' + (promptMode ? '✎' : '!') + '</div><h2></h2></div>' +
      '<div class="admin-dialog__message"></div>' +
      (promptMode ? '<input class="admin-dialog__input" type="text" autocomplete="off">' : '') +
      '<div class="admin-dialog__actions"><button type="button" class="admin-dialog__cancel">取消</button>' +
      '<button type="button" class="admin-dialog__confirm">' + (options.confirmText || '确定') + '</button></div></section>';
    root.querySelector("h2").textContent = options.title || (promptMode ? "请输入内容" : "请确认操作");
    root.querySelector(".admin-dialog__message").textContent = String(message || "");
    var input = root.querySelector(".admin-dialog__input");
    if (input) input.value = options.defaultValue || "";
    var previous = document.activeElement;
    document.body.appendChild(root);

    return new Promise(function (resolve) {
      var finished = false;
      function close(ok) {
        if (finished) return;
        finished = true;
        document.removeEventListener("keydown", onKey, true);
        root.classList.remove("open");
        setTimeout(function () { root.remove(); }, 180);
        if (previous && previous.focus) previous.focus({ preventScroll: true });
        resolve(promptMode ? (ok ? input.value : null) : Boolean(ok));
      }
      function onKey(event) {
        if (event.key === "Tab") {
          var controls = Array.from(root.querySelectorAll('button,input')).filter(function(el) { return !el.disabled; });
          var first = controls[0], last = controls[controls.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
        if (event.key === "Escape") { event.preventDefault(); close(false); }
        if (event.key === "Enter" && input && document.activeElement === input) { event.preventDefault(); close(true); }
      }
      document.addEventListener("keydown", onKey, true);
      root.querySelector(".admin-dialog__cancel").addEventListener("click", function () { close(false); });
      root.querySelector(".admin-dialog__confirm").addEventListener("click", function () { close(true); });
      root.addEventListener("click", function (event) { if (event.target === root) close(false); });
      setTimeout(function () { if (!finished) (input || root.querySelector(".admin-dialog__cancel")).focus(); }, 20);
    });
  }

  window.adminConfirm = function (message, options) { return openDialog(message, options || {}); };
  window.adminPrompt = function (message, defaultValue, options) {
    options = options || {}; options.kind = "prompt"; options.defaultValue = defaultValue || "";
    return openDialog(message, options);
  };
  window.adminToast = function (message) {
    var stack = document.querySelector(".admin-toast-stack");
    if (!stack) { stack = document.createElement("div"); stack.className = "admin-toast-stack"; document.body.appendChild(stack); }
    var item = document.createElement("div"); item.className = "admin-toast"; item.textContent = message; stack.appendChild(item);
    setTimeout(function () { item.remove(); }, 3500);
  };

  function inlineConfirmMessage(form) {
    var raw = form.getAttribute("onsubmit") || "";
    var match = raw.match(/confirm\((['\"])([\s\S]*?)\1\)/);
    return match ? match[2].replace(/\\n/g, "\n") : "";
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll('form[onsubmit*="confirm("]').forEach(function (form) {
      var message = inlineConfirmMessage(form);
      if (message) form.dataset.confirm = message;
      form.removeAttribute("onsubmit");
    });

    document.addEventListener("click", function (event) {
      var submitter = event.target.closest('button[type="submit"],input[type="submit"]');
      if (submitter && submitter.form) submitter.form._cwSubmitter = submitter;
    }, true);

    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (form.dataset.submitBypass === "1") {
        delete form.dataset.submitBypass;
        var acceptedButton = form._cwSubmitter || form.querySelector('button[type="submit"],input[type="submit"]');
        setTimeout(function () {
          if (!event.defaultPrevented && acceptedButton) { acceptedButton.disabled = true; acceptedButton.dataset.originalText = acceptedButton.textContent; acceptedButton.textContent = "处理中…"; }
        }, 0);
        return;
      }
      var message = form.dataset.confirm;
      if (message) {
        event.preventDefault();
        event.stopImmediatePropagation();
        window.adminConfirm(message, { title: "确认继续？", confirmText: "确认" }).then(function (ok) {
          if (!ok) return;
          form.dataset.submitBypass = "1";
          if (form.requestSubmit) form.requestSubmit(form._cwSubmitter || undefined); else form.submit();
        });
        return;
      }
      var button = form._cwSubmitter || form.querySelector('button[type="submit"],input[type="submit"]');
      setTimeout(function () {
        if (!event.defaultPrevented && button) { button.disabled = true; button.dataset.originalText = button.textContent; button.textContent = "处理中…"; }
      }, 0);
    }, true);

    var menu = document.getElementById("adminMenuBtn");
    var scrim = document.getElementById("adminNavScrim");
    function closeNav() { document.body.classList.remove("admin-nav-open"); }
    if (menu) menu.addEventListener("click", function () { document.body.classList.toggle("admin-nav-open"); });
    if (scrim) scrim.addEventListener("click", closeNav);
    document.querySelectorAll(".topbar nav a").forEach(function (link) { link.addEventListener("click", closeNav); });
    document.addEventListener("keydown", function (event) { if (event.key === "Escape") closeNav(); });
  });
})();
