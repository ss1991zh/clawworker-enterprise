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
      if (json && json.error === "not_logged_in") throw new Error("登录已失效，请点击重新登录");
      throw new Error((json && (json.detail || json.message)) || text || "登录已失效，请点击重新登录");
    }
    if (!response.ok) {
      throw new Error((json && (json.detail || json.message)) || text || `${response.status}`);
    }
    return json != null ? json : text;
  }

  global.ClawCore = Object.freeze({ $, esc, mdToHtml, api });
}(window));
