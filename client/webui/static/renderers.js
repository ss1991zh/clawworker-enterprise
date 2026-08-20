/* 仅生成受控 HTML 的用户端渲染器。动态值必须先经过 esc。 */
(function initClawRenderers(global) {
  "use strict";
  const { esc } = global.ClawCore;
  const FILE_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="16" y2="17"/></svg>';
  const LOCK_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>';
  const SESS_CLOCK_INLINE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
  const FOLDER_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
  const CHECK_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';
  const ICON_SVG = Object.freeze({
    doc: '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
    warn: '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    ask: '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  });

  function oneFileCard(path, name, kind) {
    const download = `/api/excel/download?path=${encodeURIComponent(path)}`;
    const icon = kind === "cipher" ? LOCK_ICON_SVG : FILE_ICON_SVG;
    const hint = kind === "cipher" ? "加密文件 · 数值列为密文" : "Excel 输出 · 明文";
    return `<div class="file-card" data-path="${esc(path)}" data-name="${esc(name)}" data-kind="${esc(kind)}">
      <div class="fc-ic ${esc(kind)}">${icon}</div><div class="fc-body"><div class="fc-nm">${esc(name)}</div>
      <div class="fc-hint">${hint}</div></div><a class="fc-btn" href="${download}" download="${esc(name)}">⬇ 下载</a></div>`;
  }

  function fileCardsHtml(message, willType) {
    const cards = [];
    if (message.enc_excel_path && message.enc_excel_name) cards.push(oneFileCard(message.enc_excel_path, message.enc_excel_name, "cipher"));
    if (message.excel_path && message.excel_name) cards.push(oneFileCard(message.excel_path, message.excel_name, "plain"));
    let decrypt = "";
    if (message.enc_excel_path && message.can_decrypt && !message.excel_path) {
      decrypt = `<button class="dec-file-btn" data-mid="${esc(message.id)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="dec-ic"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg><span>解密查看明文</span></button>`;
    }
    if (!cards.length && !decrypt) return "";
    return `<div class="file-cards"${willType ? ' data-defer-reveal="1"' : ""}>${cards.join("")}${decrypt}</div>`;
  }

  global.ClawRenderers = Object.freeze({
    SESS_CLOCK_INLINE, FOLDER_ICON_SVG, CHECK_ICON_SVG, ICON_SVG,
    oneFileCard, fileCardsHtml,
  });
}(window));
