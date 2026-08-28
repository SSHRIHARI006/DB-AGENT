from db_agent import cli


def test_literal_slash_fallback_opens_command_menu(monkeypatch):
    monkeypatch.setattr(cli, "_slash_command_menu", lambda: "/models list")
    assert cli._slash_command_menu() == "/models list"


def test_banner_mentions_current_setup_and_commands(monkeypatch, capsys):
    class Config:
        active_provider = "groq"
        providers = {
            "groq": type(
                "Entry",
                (),
                {"orchestrator_model": "orch", "worker_model": "worker"},
            )()
        }

    monkeypatch.setattr(cli, "load_provider_config", lambda _: Config())
    cli.print_banner("sqlite:///test.db", "session")
    output = capsys.readouterr().out
    assert "Provider" in output
    assert "Orchestrator" in output
    assert "Worker" in output
    assert "command menu" in output
    assert "/provider assign" in output
    assert "/models list" in output
    assert "/undo" in output
