from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[3]


def test_bot_hub_cannot_be_selected_from_bot_create_page() -> None:
    create_dialog = (ROOT_DIR / "apps/web/components/bot-create-dialog.tsx").read_text(encoding="utf-8")

    assert 'checked={botKind === "hub"}' not in create_dialog
    assert 'setBotKind("hub")' not in create_dialog
    assert 'checked={botKind === "bot"}' in create_dialog
    assert 'botKind === "hub"' in create_dialog  # 기존 처리 분기는 보존


def test_workspace_creation_label_keeps_hub_tab() -> None:
    workspace = (ROOT_DIR / "apps/web/components/bots-workspace.tsx").read_text(encoding="utf-8")
    assert "+ 봇/봇 허브 생성" not in workspace
    assert 'href="/studio/hubs"' in workspace
    assert "BOT_CREATE_CATALOGS[uiLanguage].createBot" in workspace
