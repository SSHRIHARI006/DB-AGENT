from db_agent import cli


def test_delete_session_requires_confirmation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    session_dir = tmp_path / ".db_agent" / "sessions" / "demo"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert cli._delete_session_with_confirmation("demo") is False
    assert session_dir.exists()


def test_delete_session_after_confirmation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    session_dir = tmp_path / ".db_agent" / "sessions" / "demo"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert cli._delete_session_with_confirmation("demo") is True
    assert not session_dir.exists()


def test_delete_picker_can_back_out(monkeypatch):
    monkeypatch.setattr(cli, "get_all_sessions", lambda: [{"name": "demo", "db_uri": "sqlite:///demo.db"}])
    monkeypatch.setattr(cli, "_select_menu", lambda *args, **kwargs: None)
    assert cli._choose_session_to_delete() is None
