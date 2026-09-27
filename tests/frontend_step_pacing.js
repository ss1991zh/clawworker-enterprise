"use strict";
// Exercise the real poller with a virtual clock and a minimal DOM.
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const path = require("path");
const source = fs.readFileSync(path.join(__dirname, "../client/webui/static/app.js"), "utf8");
const poller = source.slice(source.indexOf("function pollMessage(sid, mid) {"), source.indexOf("// ============ 当前会话后台同步"));
const chatContext = { window: {} };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../client/webui/static/chat-ui.js"), "utf8"), chatContext);
const realChat = chatContext.window.ClawChatUI.create({ state: {} });

async function check(status, real = false, retry = false) {
  let now = 10000, sequence = 0, replacedAt = null;
  const timers = new Map(), revealed = [];
  const box = {
    children: [], querySelectorAll: () => [],
    appendChild(child) {
      assert(Number(trace.dataset.stage) >= context.computeStageIndex(message.steps.slice(0, this.children.length + 1), "running"), "detail cannot precede its stage");
      this.children.push(child); revealed.push(now);
    },
  };
  const transitions = [{ stage: 0, time: now }];
  const trace = { dataset: new Proxy({ stage: "0" }, {
    set(target, key, value) {
      if (key === "stage" && value !== target[key]) transitions.push({ stage: Number(value), time: now });
      target[key] = value; return true;
    },
  }), querySelector: () => null, querySelectorAll: () => [], replaceWith() {} };
  const node = {
    querySelector(selector) {
      if (selector === ".decrypt-card") return replacedAt !== null && status === "awaiting_decrypt" ? {} : null;
      return selector === ".trace-steps" ? box : selector === "details.trace" ? trace : null;
    },
    replaceWith() { replacedAt = now; },
  };
  const message = { id: "m", status, steps: Array.from({ length: 6 }, (_, i) => ({ label: `step ${i}` })) };
  if (real) message.steps = realChat.displaySteps([
    "识别意图:数据分析", "加载身份列 sidecar", "调用 LLM 生成密态分析代码", "代码安全扫描通过",
    "加载密文 数据.xlsx", "密文数据已载入", "小样本校验通过", "开始执行加密数据分析 · 正在跟踪实际运算",
    "本机受控解密", "密态计算完成", "密态计算完成 · 等待解密展示授权",
  ].map(label => ({ label })));
  if (retry) message.steps.push(...[
    "结果疑似截断:数据 101 行,最大 sheet 仅 20 行 · 带反馈重新生成",
    "调用 LLM 生成密态分析代码", "加载密文 数据.xlsx", "开始执行加密数据分析",
    "本机受控解密", "密态计算完成 · 等待解密展示授权",
  ].map(label => ({ label })));
  const schedule = (fn, ms, repeat) => {
    const id = ++sequence; timers.set(id, { fn, due: now + ms, ms, repeat }); return id;
  };
  const context = {
    Date: { now: () => now },
    state: { pollingMids: new Set(), currentSession: { messages: [message] }, pendingCipher: true },
    document: {
      querySelector: selector => selector.endsWith("details.trace") ? trace : node,
      createElement: () => ({ classList: { add() {} } }),
    },
    $: () => ({ scrollHeight: 0, scrollTop: 0, clientHeight: 0 }),
    api: async () => {
      if (status === "unauthorized") throw Object.assign(new Error("expired"), { status: 401 });
      return message;
    },
    ui: { alert: async () => {} },
    window: { location: { assign: url => { assert.strictEqual(url, "/login"); replacedAt = now; } } },
    displaySteps: steps => steps,
    stepHtml: s => s.label, computeStageIndex: (steps, phase) => phase === "done" ? 4 : Math.min(3, Math.floor(Math.max(0, steps.length - 1) / 2)),
    renderMessage: () => node, setRunning() {}, loadSessions() {},
    setTimeout: (fn, ms) => schedule(fn, ms, false),
    setInterval: (fn, ms) => schedule(fn, ms, true),
    clearInterval: id => timers.delete(id),
    clearTimeout: id => timers.delete(id),
  };
  if (real) context.computeStageIndex = realChat.computeStageIndex;
  vm.runInNewContext(poller + '\npollMessage("s", "m");', context);
  for (let i = 0; i < 80 && replacedAt === null; i++) {
    const [id, timer] = [...timers.entries()].sort((a, b) => a[1].due - b[1].due)[0];
    now = timer.due;
    if (timer.repeat) timer.due += timer.ms; else timers.delete(id);
    await timer.fn();
  }
  if (status === "unauthorized") {
    assert.strictEqual(context.state.pollingMids.size, 0);
    assert.strictEqual(timers.size, 0, "expired login must not retry polling");
    assert(replacedAt !== null, "login navigation required");
    return;
  }
  assert.strictEqual(revealed.length, message.steps.length, `${status}: all steps displayed`);
  for (let i = 1; i < revealed.length; i++) assert(revealed[i] - revealed[i - 1] >= 500);
  if (status === "awaiting_decrypt") {
    assert(replacedAt - revealed.at(-1) >= 500, "authorization waits for the final detail");
    assert(replacedAt - transitions.at(-1).time >= 1500, "authorization waits for the final stage");
    assert.strictEqual(context.state.pollingMids.size, 1, "authorization keeps polling");
    return;
  }
  assert(replacedAt !== null && replacedAt - revealed.at(-1) >= 500, `${status}: last step remains visible`);
  assert.strictEqual(transitions.at(-1).stage, status === "done" ? (real ? 5 : 4) : real ? 4 : 2);
  for (let i = 1; i < transitions.length; i++) {
    assert(transitions[i].stage === transitions[i - 1].stage + 1 || (retry && transitions[i].stage === 0));
    assert(transitions[i].time - transitions[i - 1].time >= 1500, "stage dwell >= 1500ms");
  }
  assert(replacedAt - transitions.at(-1).time >= 1500, "final stage remains visible");
  if (retry) assert(transitions.slice(1).some(t => t.stage === 0), "retry returns to understanding");
  if (status !== "awaiting_decrypt") assert.strictEqual(context.state.pollingMids.size, 0);
}
(async () => {
  for (const status of ["done", "failed", "cancelled", "needs_cipher", "awaiting_decrypt"]) await check(status);
  await check("done", true);
  await check("awaiting_decrypt", true);
  await check("done", true, true);
  await check("unauthorized");
  console.log("Step pacing passed: details >= 500ms, sequential stages >= 1500ms, terminal and decrypt gates.");
})().catch(error => { console.error(error); process.exitCode = 1; });
