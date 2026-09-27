"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

global.window = global;
global.document = {
  getElementById: () => null,
  querySelector: () => null,
};

const staticDir = path.join(__dirname, "..", "client", "webui", "static");
for (const name of [
  "core.js", "renderers.js", "database-ui.js", "files-ui.js",
  "settings-ui.js", "skills-ui.js", "tasks-ui.js", "chat-ui.js",
]) {
  vm.runInThisContext(fs.readFileSync(path.join(staticDir, name), "utf8"), { filename: name });
}

const noop = () => undefined;
const deps = new Proxy({}, {
  get(_target, key) {
    if (key === "state") return {};
    if (key === "title") return () => "";
    if (key === "FOLDER_ICON_SVG" || key === "CHECK_ICON_SVG" || key === "SESS_CLOCK_INLINE") return "";
    if (key === "ICON_SVG") return {};
    return noop;
  },
});

for (const namespace of [
  "ClawDatabaseUI", "ClawFilesUI", "ClawSettingsUI", "ClawSkillsUI",
  "ClawTasksUI", "ClawChatUI",
]) {
  const api = global[namespace];
  if (!api || typeof api.create !== "function") throw new Error(`${namespace} was not initialized`);
  const feature = api.create(deps);
  if (!feature || Object.keys(feature).length === 0) throw new Error(`${namespace} returned no feature methods`);
}

const escaped = global.ClawCore.mdToHtml('<img src=x onerror="boom">');
if (escaped.includes("<img")) throw new Error("markdown renderer allowed raw HTML");

document.createElement = () => ({ dataset: {}, querySelector: () => null, querySelectorAll: () => [] });
const chat = ClawChatUI.create({ state: { typedMids: new Set() }, esc: ClawCore.esc,
  ICON_SVG: {}, mdToHtml: ClawCore.mdToHtml });
const waitingMessage = { id: "gate", role: "assistant", status: "awaiting_decrypt",
  steps: [{ label: "密态计算完成 · 等待解密展示授权(2 个 sheet)" }], summary: "PRIVATE_SUMMARY_MUST_NOT_RENDER" };
const initialWaitingHtml = chat.renderMessage(waitingMessage).innerHTML;
if (initialWaitingHtml.includes('data-choice="decrypt"')) throw new Error("authorization appeared before playback");
const waitingHtml = chat.renderMessage(waitingMessage, { authorizationReady: true }).innerHTML;
if (!waitingHtml.includes('data-choice="decrypt"') || !waitingHtml.includes('data-choice="keep_encrypted"')) throw new Error("authorization choices missing");
const cancelledHtml = chat.renderMessage({ ...waitingMessage, status: "cancelled" }).innerHTML;
if (cancelledHtml.includes(waitingMessage.summary) || cancelledHtml.includes('class="ask-card"')) throw new Error("cancelled run rendered analysis as warning");
if (!cancelledHtml.includes("恢复问题与数据")) throw new Error("cancelled run missing recovery action");
if (cancelledHtml.includes("data-copy-message") || initialWaitingHtml.includes("data-copy-message")) throw new Error("unfinished message exposes hover actions");
if (!waitingHtml.includes('data-stage="0"')) throw new Error("initial authorization render skipped playback");
const playbackState = { typedMids: new Set() };
const playbackChat = ClawChatUI.create({ state: playbackState, esc: ClawCore.esc, ICON_SVG: {} });
playbackState.tracePlayback.set("gate", { count: 1, stage: 1 });
const rerendered = playbackChat.renderMessage(waitingMessage).innerHTML;
if (!rerendered.includes('data-stage="1"')) throw new Error("rerender lost displayed stage");
const filtered = playbackChat.displaySteps([
  { label: "加载身份列 sidecar · 101 行" }, { label: "代码生成 · 加载技能文档:demo" },
  { label: "代码安全扫描通过" }, { label: "本机受控解密" }, { kind: "error", label: "加载身份列 sidecar 失败" },
]);
if (filtered.length !== 3) throw new Error("filter removed important operations or retained internal noise");
if (!playbackChat.stepHtml({ label: "本机受控解密" }).includes("不是纯密文运算")) throw new Error("decryption description is misleading");
