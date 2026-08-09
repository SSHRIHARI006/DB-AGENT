from types import SimpleNamespace

from db_agent import cli


def test_windows_get_char_translates_extended_arrow_keys(monkeypatch):
    keys = iter(["\x00", "H", "\xe0", "P", "x", "\r", "\x03"])
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli, "msvcrt", SimpleNamespace(getwch=lambda: next(keys)), raising=False)

    assert cli.get_char() == "\x1b[A"
    assert cli.get_char() == "\x1b[B"
    assert cli.get_char() == "x"
    assert cli.get_char() == "\r"
    assert cli.get_char() == "\x03"


def test_windows_get_char_preserves_unknown_extended_keys(monkeypatch):
    keys = iter(["\x00", "K"])
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli, "msvcrt", SimpleNamespace(getwch=lambda: next(keys)), raising=False)

    assert cli.get_char() == "K"
