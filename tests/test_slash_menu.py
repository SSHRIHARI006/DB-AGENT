from db_agent import cli


def test_slash_as_first_character_opens_command_menu(monkeypatch):
    keys = iter(["/", "\r"])
    monkeypatch.setattr(cli, "get_char", lambda: next(keys))
    monkeypatch.setattr(cli, "_slash_command_menu", lambda: "/models list")
    assert cli.get_user_input() == "/models list"


def test_normal_input_remains_a_natural_language_query(monkeypatch):
    keys = iter(["h", "i", "\r"])
    monkeypatch.setattr(cli, "get_char", lambda: next(keys))
    assert cli.get_user_input() == "hi"


def test_command_menu_back_returns_empty_command(monkeypatch):
    monkeypatch.setattr(cli, "_select_menu", lambda *args, **kwargs: None)
    assert cli._slash_command_menu() == ""
