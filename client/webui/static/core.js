/* Clawworker 用户端公共安全渲染与网络访问工具。 */
(function initClawCore(global) {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));

  function mdToHtml(src) {
    if (!src) return "";
    let text = esc(src);
    const blocks = [];
    const inline = [];
    const stash = (html) => { inline.push(html); return `@@I${inline.length - 1}@@`; };
    text = text.replace(/```[^\n`]*\n?([\s\S]*?)```/g, (_, code) => {
      blocks.push(code.replace(/\n+$/, ""));
      return `@@B${blocks.length - 1}@@`;
    });
    text = text.replace(/`([^`\n]+)`/g, (_, code) => stash(`<code>${code}</code>`));
    text = text.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) => stash(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`));
    text = text.replace(/(^|[\s(（])(https?:\/\/[^\s<)）]+)/g,
      (_, prefix, url) => `${prefix}${stash(`<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`)}`);
    text = text.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
      .replace(/(^|[^_])_([^_\n]+)_(?!_)/g, "$1<em>$2</em>");

    const out = [];
    let list = null;
    const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const line of text.split("\n")) {
      const placeholder = line.match(/^@@B(\d+)@@$/);
      if (placeholder) {
        closeList();
        out.push(`<pre><code>${blocks[+placeholder[1]]}</code></pre>`);
        continue;
      }
      let match;
      if ((match = line.match(/^(#{1,6})\s+(.*)$/))) {
        closeList();
        const level = Math.min(match[1].length, 6);
        out.push(`<h${level}>${match[2]}</h${level}>`);
      } else if (/^\s*([-*+])\s+/.test(line)) {
        if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
        out.push(`<li>${line.replace(/^\s*[-*+]\s+/, "")}</li>`);
      } else if (/^\s*\d+\.\s+/.test(line)) {
        if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
        out.push(`<li>${line.replace(/^\s*\d+\.\s+/, "")}</li>`);
      } else if (/^\s*>\s?/.test(line)) {
        closeList(); out.push(`<blockquote>${line.replace(/^\s*>\s?/, "")}</blockquote>`);
      } else if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        closeList(); out.push("<hr>");
      } else if (line.trim() === "") {
        closeList();
      } else {
        closeList(); out.push(`<p>${line}</p>`);
      }
    }
    closeList();
    let html = out.join("").replace(/@@I(\d+)@@/g, (_, index) => inline[+index] || "");
    html = html.replace(/<\/a>\s*<a /g, '</a><span class="link-sep">·</span><a ');
    const linkRun = '(?:<a\\b[^>]*>[^<]*<\\/a>(?:<span class="link-sep">·<\\/span>)?)+';
    return html.replace(new RegExp(`(${linkRun})\\s*([。.!?;！?;])`, "g"),
      (_, run, punctuation) => `${punctuation} ${run}`);
  }

  async function api(method, path, body, isMultipart = false) {
    const options = { method, headers: {} };
    const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
    if (csrf) options.headers["X-CSRF-Token"] = csrf;
    if (body) {
      if (isMultipart) options.body = body;
      else {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }
    }
    const response = await fetch(path, options);
    let json = null;
    let text = "";
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      try { json = await response.json(); } catch (_) {}
    } else {
      try { text = await response.text(); } catch (_) {}
    }
    if (response.status === 401) {
      const error = new Error((json && (json.detail || json.message)) || "登录已失效，请重新登录");
      error.status = 401;
      throw error;
    }
    if (!response.ok) {
      throw new Error((json && (json.detail || json.message)) || text || `${response.status}`);
    }
    return json != null ? json : text;
  }

  let activeDialog = null;

  function toast(message, type = "info", duration = 3600) {
    let stack = document.getElementById("cwToastStack");
    if (!stack) {
      stack = document.createElement("div");
      stack.id = "cwToastStack";
      stack.className = "cw-toast-stack";
      stack.setAttribute("aria-live", "polite");
      document.body.appendChild(stack);
    }
    const item = document.createElement("div");
    item.className = `cw-toast-item ${type}`;
    item.setAttribute("role", type === "error" ? "alert" : "status");
    item.innerHTML = `<span class="cw-toast-item__icon" aria-hidden="true"></span><span class="cw-toast-item__text"></span><button type="button" class="cw-toast-item__close" aria-label="关闭">×</button>`;
    item.querySelector(".cw-toast-item__text").textContent = String(message || "");
    const remove = () => {
      item.classList.remove("show");
      window.setTimeout(() => item.remove(), 220);
    };
    item.querySelector(".cw-toast-item__close").addEventListener("click", remove);
    stack.appendChild(item);
    requestAnimationFrame(() => item.classList.add("show"));
    window.setTimeout(remove, Math.max(1800, duration));
    return item;
  }

  function openDialog(message, options = {}) {
    if (activeDialog) activeDialog(false);
    const previousFocus = document.activeElement;
    const kind = options.kind || "info";
    const title = options.title || (kind === "danger" ? "请确认操作" : kind === "prompt" ? "需要填写" : "提示");
    const root = document.createElement("div");
    root.className = "cw-dialog-mask open";
    root.innerHTML = `
      <section class="cw-dialog ${kind}" role="dialog" aria-modal="true" aria-labelledby="cwDialogTitle">
        <div class="cw-dialog__icon" aria-hidden="true"></div>
        <div class="cw-dialog__content">
          <h2 id="cwDialogTitle"></h2>
          <div class="cw-dialog__message"></div>
          ${kind === "prompt" ? '<input class="cw-dialog__input" type="text" autocomplete="off">' : ""}
        </div>
        <div class="cw-dialog__actions">
          ${options.cancelText !== null ? '<button type="button" class="cw-dialog__btn secondary" data-dialog-cancel></button>' : ""}
          <button type="button" class="cw-dialog__btn primary" data-dialog-confirm></button>
        </div>
      </section>`;
    root.querySelector("#cwDialogTitle").textContent = title;
    root.querySelector(".cw-dialog__message").textContent = String(message || "");
    const confirmButton = root.querySelector("[data-dialog-confirm]");
    const cancelButton = root.querySelector("[data-dialog-cancel]");
    confirmButton.textContent = options.confirmText || "确定";
    if (kind === "danger") confirmButton.classList.add("danger");
    if (cancelButton) cancelButton.textContent = options.cancelText || "取消";
    const input = root.querySelector(".cw-dialog__input");
    if (input) input.value = options.defaultValue || "";

    document.body.appendChild(root);
    document.body.classList.add("cw-dialog-open");
    return new Promise((resolve) => {
      let finished = false;
      const finish = (accepted) => {
        if (finished) return;
        finished = true;
        activeDialog = null;
        document.removeEventListener("keydown", onKeydown, true);
        root.classList.remove("open");
        document.body.classList.remove("cw-dialog-open");
        window.setTimeout(() => root.remove(), 180);
        if (previousFocus && previousFocus.focus) previousFocus.focus({ preventScroll: true });
        resolve(kind === "prompt" ? (accepted ? input.value : null) : Boolean(accepted));
      };
      activeDialog = finish;
      const onKeydown = (event) => {
        if (event.key === "Tab") {
          const controls = Array.from(root.querySelectorAll('button, input')).filter(el => !el.disabled);
          const first = controls[0], last = controls[controls.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
        if (event.key === "Escape" && options.cancelText !== null) { event.preventDefault(); finish(false); }
        if (event.key === "Enter" && input && document.activeElement === input) { event.preventDefault(); finish(true); }
      };
      document.addEventListener("keydown", onKeydown, true);
      confirmButton.addEventListener("click", () => finish(true));
      if (cancelButton) cancelButton.addEventListener("click", () => finish(false));
      root.addEventListener("click", (event) => { if (event.target === root && cancelButton) finish(false); });
      window.setTimeout(() => { if (!finished) (input || cancelButton || confirmButton).focus(); }, 20);
    });
  }

  const ui = Object.freeze({
    toast,
    alert: (message, options = {}) => openDialog(message, { ...options, cancelText: null, kind: options.kind || "info" }),
    confirm: (message, options = {}) => openDialog(message, { ...options, kind: options.danger ? "danger" : "confirm" }),
    prompt: (message, defaultValue = "", options = {}) => openDialog(message, { ...options, defaultValue, kind: "prompt" }),
  });

  global.ClawCore = Object.freeze({ $, esc, mdToHtml, api, ui });
}(window));
