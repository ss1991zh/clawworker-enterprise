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
