from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "client" / "webui" / "static"


def test_frontend_modules_load_before_orchestrator():
    html = (ROOT / "client" / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
    names = [
        "core.js", "renderers.js", "database-ui.js", "files-ui.js",
        "settings-ui.js", "skills-ui.js", "tasks-ui.js", "chat-ui.js", "app.js",
    ]
    positions = [html.index(f'/static/{name}') for name in names]
    assert positions == sorted(positions)


def test_frontend_orchestrator_is_split_by_feature():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert len(app_js.splitlines()) < 1500
    for namespace in (
        "ClawCore", "ClawRenderers", "ClawDatabaseUI", "ClawFilesUI",
        "ClawSettingsUI", "ClawSkillsUI", "ClawTasksUI", "ClawChatUI",
    ):
        assert namespace in app_js


def test_overview_timer_is_owned_by_tasks_module():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    tasks_js = (STATIC / "tasks-ui.js").read_text(encoding="utf-8")
    assert "clearInterval(_gsTimer)" not in app_js
    assert "openOverview, leaveOverview" in app_js
    assert "function leaveOverview()" in tasks_js
    assert "openOverview, leaveOverview" in tasks_js


def test_send_stop_icon_is_exported_to_orchestrator_scope():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    renderers_js = (STATIC / "renderers.js").read_text(encoding="utf-8")
    chat_js = (STATIC / "chat-ui.js").read_text(encoding="utf-8")

    assert "CHECK_ICON_SVG, STOP_ICON_SVG, ICON_SVG" in app_js
    assert "CHECK_ICON_SVG, STOP_ICON_SVG, ICON_SVG" in renderers_js
    assert "const STOP_ICON_SVG" not in chat_js


def test_encrypted_computation_is_shown_live_instead_of_result_proof_card():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    chat_js = (STATIC / "chat-ui.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    assert "加密计算过程 · 执行中" in chat_js
    assert "computeStageIndex" in app_js
    assert "compute-stages" in css
    assert "securityProofHtml(m)" not in chat_js


def test_core_renderer_escapes_dynamic_text_before_markdown_markup():
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    assert "let text = esc(src)" in core
    assert 'rel="noopener noreferrer"' in core
    assert "Object.freeze({ $, esc, mdToHtml, api, ui })" in core


def test_client_uses_application_dialogs_instead_of_browser_alerts():
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    assert "cw-dialog-mask" in core
    assert "const ui = Object.freeze" in core
    for name in ("app.js", "chat-ui.js", "files-ui.js", "settings-ui.js", "skills-ui.js", "tasks-ui.js"):
        source = (STATIC / name).read_text(encoding="utf-8")
        assert "window.confirm(" not in source
        assert "window.alert(" not in source


def test_static_modals_use_shared_css_positioning():
    html = (ROOT / "client" / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert 'style="position:relative;"' not in html
    assert "position: relative;" in css
