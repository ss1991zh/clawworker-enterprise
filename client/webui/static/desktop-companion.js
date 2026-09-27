(function () {
  "use strict";
  const button = document.getElementById("companionToggle");
  if (!button) return;
  let busy = false;
  function paint(state) {
    button.disabled = !state.available;
    button.setAttribute("aria-checked", String(!!state.enabled && !!state.available));
    button.title = state.available ? "显示或隐藏桌面龙虾助手" : "桌面助手不可用，请重新打开桌面客户端";
  }
  async function refresh() {
    if (!window.pywebview?.api?.companion_state || busy) return;
    try { paint(await window.pywebview.api.companion_state()); }
    catch (_) { button.disabled = true; button.title = "桌面助手连接失败，请重新打开客户端"; }
  }
  button.addEventListener("click", async () => {
    if (busy) return;
    busy = true; button.disabled = true;
    try { paint(await window.pywebview.api.set_companion(button.getAttribute("aria-checked") !== "true")); }
    catch (_) { window.ClawCore.ui.toast("桌面助手切换失败，请重试", "error"); button.disabled = false; }
    finally { busy = false; }
  });
  window.addEventListener("pywebviewready", refresh);
  window.addEventListener("companionchange", refresh);
  refresh();
}());
