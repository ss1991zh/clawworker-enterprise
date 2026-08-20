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


def test_core_renderer_escapes_dynamic_text_before_markdown_markup():
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    assert "let text = esc(src)" in core
    assert 'rel="noopener noreferrer"' in core
    assert "Object.freeze({ $, esc, mdToHtml, api })" in core


def test_static_modals_use_shared_css_positioning():
    html = (ROOT / "client" / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert 'style="position:relative;"' not in html
    assert "position: relative;" in css
