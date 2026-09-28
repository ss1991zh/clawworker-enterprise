(function () {
  "use strict";
  const main = document.getElementById("main");
  const chat = document.getElementById("chat");
  if (!main || !chat) return;
  const nav = document.createElement("nav");
  nav.className = "question-nav";
  nav.setAttribute("aria-label", "本会话的问题导航");
  nav.hidden = true;
  main.parentElement.appendChild(nav);
  const tooltip = document.createElement("div");
  tooltip.className = "question-nav-preview";
  tooltip.hidden = true;
  document.body.appendChild(tooltip);
  let entries = [], signature = "", frame = 0;

  function replySummary(node) {
    const parts = [];
    for (let sibling = node.nextElementSibling; sibling; sibling = sibling.nextElementSibling) {
      if (sibling.matches(".msg.user")) break;
      if (!sibling.matches(".msg.bot")) continue;
      const body = sibling.querySelector(".bubble.md, .err-text, .ask-card");
      if (body?.textContent.trim()) parts.push(body.textContent.replace(/▍/g, "").trim());
      else if (sibling.querySelector(".decrypt-card")) parts.push("计算结果已准备好，等待选择展示方式。");
      else if (sibling.querySelector(".run-pill")) parts.push("正在处理这个问题，回复尚未完成。");
    }
    return parts.join(" ").replace(/\s+/g, " ") || "暂无回复";
  }
  function wave(center) {
    entries.forEach(entry => {
      const rect = entry.button.getBoundingClientRect();
      const distance = Math.abs(rect.top + rect.height / 2 - center) / 14;
      entry.button.style.setProperty("--tick-width", `${8 + 20 * Math.exp(-distance * distance / 3.5)}px`);
    });
  }
  function resetWave() { entries.forEach(entry => entry.button.style.removeProperty("--tick-width")); }

  function showPreview(entry, button) {
    const question = document.createElement("div");
    question.className = "question-nav-question";
    question.textContent = entry.text;
    const answer = document.createElement("div");
    answer.className = "question-nav-answer";
    answer.textContent = replySummary(entry.node);
    tooltip.replaceChildren(question, answer);
    tooltip.hidden = false;
    const rect = button.getBoundingClientRect();
    tooltip.style.right = Math.max(40, window.innerWidth - rect.left + 10) + "px";
    tooltip.style.top = Math.max(8, Math.min(rect.top - tooltip.offsetHeight / 2, window.innerHeight - tooltip.offsetHeight - 8)) + "px";
  }
  function hidePreview() { tooltip.hidden = true; }
  function highlight() {
    frame = 0;
    if (!entries.length || nav.hidden) return;
    const baseline = main.getBoundingClientRect().top + 70;
    let active = 0;
    entries.forEach((entry, i) => { if (entry.node.getBoundingClientRect().top <= baseline) active = i; });
    entries.forEach((entry, i) => {
      entry.button.classList.toggle("active", i === active);
      if (i === active) entry.button.setAttribute("aria-current", "true");
      else entry.button.removeAttribute("aria-current");
    });
  }
  function scheduleHighlight() { if (!frame) frame = requestAnimationFrame(highlight); }
  function rebuild() {
    const nodes = [...chat.querySelectorAll(".msg.user")];
    nav.hidden = !nodes.length || chat.classList.contains("ovmode");
    const next = JSON.stringify(nodes.map(node => [node.dataset.mid, node.querySelector(".bubble")?.textContent || "（附件消息）"]));
    if (next === signature) {
      entries.forEach((entry, i) => { entry.node = nodes[i]; });
      if (nav.hidden) hidePreview();
      scheduleHighlight(); return;
    }
    signature = next; hidePreview(); nav.replaceChildren();
    entries = nodes.map((node, index) => {
      const text = node.querySelector(".bubble")?.textContent || "（附件消息）";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "question-nav-tick";
      button.setAttribute("aria-label", `第 ${index + 1} 个问题：${text}`);
      const entry = { node, button, text };
      button.addEventListener("mouseenter", () => showPreview(entry, button));
      button.addEventListener("focus", () => { showPreview(entry, button); const r = button.getBoundingClientRect(); wave(r.top + r.height / 2); });
      button.addEventListener("mouseleave", hidePreview);
      button.addEventListener("blur", () => { hidePreview(); resetWave(); });
      button.addEventListener("click", () => {
        const top = main.scrollTop + entry.node.getBoundingClientRect().top - main.getBoundingClientRect().top - 20;
        main.scrollTo({ top: Math.max(0, top), behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
        hidePreview();
      });
      nav.appendChild(button);
      return entry;
    });
    scheduleHighlight();
  }
  main.addEventListener("scroll", scheduleHighlight, { passive: true });
  nav.addEventListener("mousemove", e => wave(e.clientY), { passive: true });
  nav.addEventListener("mouseleave", () => { hidePreview(); resetWave(); });
  nav.addEventListener("scroll", () => { hidePreview(); resetWave(); }, { passive: true });
  window.addEventListener("resize", () => { hidePreview(); scheduleHighlight(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") hidePreview(); });
  new MutationObserver(rebuild).observe(chat, { childList: true, attributes: true, attributeFilter: ["class"] });
  new ResizeObserver(scheduleHighlight).observe(chat);
  rebuild();
}());
